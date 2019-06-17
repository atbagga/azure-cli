# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

from __future__ import print_function
import os
import re
import math
import platform
import datetime

try:
    from urllib.parse import urlencode  # python 3
except ImportError:
    from urllib import urlencode  # python 2

from collections import namedtuple

from knack.log import get_logger
from knack.prompting import prompt, NoTTYException
from knack.util import CLIError

from azure.cli.core.extension._resolve import resolve_project_url_from_index
from azure.cli.core.util import get_az_version_string, open_page_in_browser
from azure.cli.core.azlogging import _UNKNOWN_COMMAND, _CMD_LOG_LINE_PREFIX

_ONE_MIN_IN_SECS = 60

_ONE_HR_IN_SECS = 3600

# see: https://stackoverflow.com/questions/417142/what-is-the-maximum-length-of-a-url-in-different-browsers
_MAX_URL_LENGTH = 2035


logger = get_logger(__name__)

_MSG_THNK = 'Thanks for your feedback!'

_GET_STARTED_URL = "aka.ms/azcli/get-started"
_QUESTIONS_URL = "aka.ms/azcli/questions"

_CLI_ISSUES_URL = "aka.ms/azcli/issues"
_RAW_CLI_ISSUES_URL = "https://github.com/Azure/azure-cli/issues/new"

_EXTENSIONS_ISSUES_URL = "aka.ms/azcli/ext/issues"
_RAW_EXTENSIONS_ISSUES_URL = "https://github.com/Azure/azure-cli-extensions/issues/new"

_MSG_INTR = \
    '\nWe appreciate your feedback!\n\n' \
    'For more information on getting started, visit: {}\n' \
    'If you have questions, visit our Stack Overflow page: {}\n'\
    .format(_GET_STARTED_URL, _QUESTIONS_URL)

_MSG_CMD_ISSUE = "\nEnter the number of the command you would like to create an issue for. Enter q to quit: "

_MSG_ISSUE = "Would you like to create an issue? Enter Y or N: "

_ISSUES_TEMPLATE_PREFIX = """

BEGIN TEMPLATE
===============
**A browser has been opened to {} to create an issue.**
**You can also run `az feedback --verbose` to emit the full output to stderr.**
"""

_ISSUES_TEMPLATE = """

### **This is autogenerated. Please review and update as needed.**

## Describe the bug

**Command Name**
`{command_name}`

**Errors:**
{errors_string}

## To Reproduce:
Steps to reproduce the behavior. Note that argument values have been redacted, as they may contain sensitive information.

- _Put any pre-requisite steps here..._
- `{executed_command}`

## Expected Behavior

## Environment Summary
```
{platform}
{python_info}
{shell}

{cli_version}
```
## Additional Context

<!--Please don't remove this:-->
{auto_gen_comment}

"""

_AUTO_GEN_COMMENT = "<!--auto-generated-->"

_LogMetadataType = namedtuple('LogMetadata', ['cmd', 'seconds_ago', 'file_path', 'p_id'])


class CommandLogFile(object):
    _LogRecordType = namedtuple("LogRecord", ["p_id", "date_time", "level", "logger", "log_msg"])
    UNKNOWN_CMD = "Unknown"

    def __init__(self, log_file_path, time_now=None):

        if (time_now is not None) and (not isinstance(time_now, datetime.datetime)):
            raise TypeError("Expected type {} for time_now, instead received {}.".format(datetime.datetime, type(time_now)))  # pylint: disable=line-too-long

        if not os.path.isfile(log_file_path):
            raise ValueError("File {} is not an existing file.".format(log_file_path))

        self._command_name = None
        self._log_file_path = log_file_path

        if time_now is None:
            self._time_now = datetime.datetime.now()
        else:
            self._time_now = time_now

        self._metadata = self._get_command_metadata_from_file()
        self._data = None

    @property
    def metadata_tup(self):
        return self._metadata

    @property
    def command_data_dict(self):
        if not self._data:
            self._data = self._get_command_data_from_metadata()
        return self._data

    def get_command_name_str(self):
        if self._command_name is not None:
            return self._command_name  # attempt to return cached command name

        if not self.metadata_tup:
            return ""

        args = self.command_data_dict.get("command_args", "")

        if self.metadata_tup.cmd != self.UNKNOWN_CMD:
            self._command_name = self.metadata_tup.cmd

            if "-h" in args or "--help" in args:
                self._command_name += " --help"
        else:
            self._command_name = self.UNKNOWN_CMD
            if args:
                command_args = args if len(args) < 16 else args[:11] + " ..."
                command_args = command_args.replace("=", "").replace("{", "").replace("}", "")
                self._command_name = "{} ({}) ".format(self._command_name, command_args)

        return self._command_name

    def get_command_status(self):
        if not self.command_data_dict:
            return ""

        was_successful = self.command_data_dict.get("success", None)
        if was_successful is None:
            success_msg = "RUNNING"
        else:
            success_msg = "SUCCESS" if was_successful else "FAILURE"
        return success_msg

    def failed(self):
        if not self.command_data_dict:
            return False

        return not self.command_data_dict.get("success", True)

    def get_command_time_str(self):
        if not self.metadata_tup:
            return ""

        total_seconds = self.metadata_tup.seconds_ago

        time_delta = datetime.timedelta(seconds=total_seconds)
        logger.debug("%s time_delta", time_delta)

        if time_delta.days > 0:
            time_str = "Ran: {} days ago".format(time_delta.days)
        elif total_seconds > _ONE_HR_IN_SECS:
            hrs, secs = divmod(total_seconds, _ONE_HR_IN_SECS)
            logger.debug("%s hrs, %s secs", hrs, secs)
            hrs = int(hrs)
            mins = math.floor(secs / _ONE_MIN_IN_SECS)
            time_str = "Ran: {} hrs {:02} mins ago".format(hrs, mins)
        elif total_seconds > _ONE_MIN_IN_SECS:
            time_str = "Ran: {} mins ago".format(math.floor(total_seconds / _ONE_MIN_IN_SECS))
        else:
            time_str = "Ran: {} secs ago".format(math.floor(total_seconds))

        return time_str

    def _get_command_metadata_from_file(self):
        if not self._log_file_path:
            return None

        time_now = datetime.datetime.now() if not self._time_now else self._time_now

        try:
            _, file_name = os.path.split(self._log_file_path)
            poss_date, poss_time, poss_command, poss_pid, _ = file_name.split(".")
            date_time_stamp = datetime.datetime.strptime("{}-{}".format(poss_date, poss_time), "%Y-%m-%d-%H-%M-%S")
            command = "az " + poss_command.replace("_", " ") if poss_command != _UNKNOWN_COMMAND else self.UNKNOWN_CMD  # pylint: disable=line-too-long
        except ValueError as e:
            logger.debug("Could not load metadata from file name %s.", self._log_file_path)
            logger.debug(e)
            return None

        difference = time_now - date_time_stamp

        total_seconds = difference.total_seconds()

        return _LogMetadataType(cmd=command, seconds_ago=total_seconds, file_path=self._log_file_path, p_id=int(poss_pid))  # pylint: disable=line-too-long

    def _get_command_data_from_metadata(self):  # pylint: disable=too-many-statements
        def _get_log_record_list(log_fp, p_id):
            """
             Get list of records / messages in the log file
            :param log_fp: log file object
            :param p_id: process id of command
            :return:
            """
            prev_record = None
            log_record_list = []
            for line in log_fp:
                # attempt to extract log data
                log_record = CommandLogFile._get_info_from_log_line(line, p_id)

                if log_record:  # if new record parsed, add old record to the list
                    if prev_record:
                        log_record_list.append(prev_record)
                    prev_record = log_record
                elif prev_record:  # otherwise this is a continuation of a log record, add to prev record
                    new_log_msg = prev_record.log_msg + line
                    prev_record = CommandLogFile._LogRecordType(p_id=prev_record.p_id, date_time=prev_record.date_time,
                                                                # pylint: disable=line-too-long
                                                                level=prev_record.level, logger=prev_record.logger,
                                                                log_msg=new_log_msg)
            if prev_record:
                log_record_list.append(prev_record)
            return log_record_list

        if not self.metadata_tup:
            return {}

        _EXT_NAME_PREFIX = "extension name:"
        _EXT_VERS_PREFIX = "extension version:"

        file_name = self.metadata_tup.file_path
        p_id = self.metadata_tup.p_id

        try:
            with open(file_name, 'r') as log_fp:
                log_record_list = _get_log_record_list(log_fp, p_id)
        except IOError:
            logger.debug("Failed to open command log file %s", file_name)
            return {}

        if not log_record_list:
            logger.debug("No command log messages found in file %s", file_name)
            return {}

        log_data = {}
        # 1. Figure out whether the command was successful or not. Last log record should be the exit code
        try:
            status_msg = log_record_list[-1].log_msg.strip()
            if status_msg.startswith("exit code"):
                idx = status_msg.index(":")  # raises ValueError
                exit_code = int(log_record_list[-1].log_msg[idx + 1:].strip())
                log_data["success"] = bool(not exit_code)
        except (IndexError, ValueError):
            logger.debug("Couldn't extract exit code from command log %s.", file_name)

        # 2. If there are any errors, this is a failed command. Log the errors
        # 3. Also get extension information.
        for record in log_record_list:
            errors = log_data.setdefault("errors", [])  # log_data["errors"]
            if record.level.lower() == "error":
                log_data["success"] = False
                errors.append(record.log_msg)

            poss_ext_msg = record.log_msg.strip()
            if record.level.lower() == "info":
                if poss_ext_msg.startswith(_EXT_NAME_PREFIX):
                    log_data["extension_name"] = poss_ext_msg[len(_EXT_NAME_PREFIX):].strip()
                elif poss_ext_msg.startswith(_EXT_VERS_PREFIX):
                    log_data["extension_version"] = poss_ext_msg[len(_EXT_VERS_PREFIX):].strip()

        # 4. Get command args string. from first record
        try:
            command_args_msg = log_record_list[0].log_msg.strip()
            if command_args_msg.lower().startswith("command args:"):
                idx = command_args_msg.index(":")
                log_data["command_args"] = command_args_msg[idx + 1:].strip()
            else:
                raise ValueError
        except (IndexError, ValueError):
            logger.debug("Couldn't get command args from command log %s.", file_name)

        return log_data

    @staticmethod
    def _get_info_from_log_line(line, p_id):
        """

        Extract log line information based on the following command log format in azlogging.py

        lfmt = logging.Formatter('%(process)d | %(created)s | %(levelname)s | %(name)s | %(message)s')

        :param line: the line from the log file.
        :return: returned parsed line information or None
        """

        if not line.startswith(_CMD_LOG_LINE_PREFIX):
            return None

        line = line[len(_CMD_LOG_LINE_PREFIX):]
        parts = line.split("|", 4)

        if len(parts) != 5:  # there must be 5 items
            return None

        for i, part in enumerate(parts):
            parts[i] = part.strip()
            if i == 0:
                parts[0] = int(parts[0])
                if parts[0] != p_id:  # ensure that this is indeed a valid log.
                    return None

        # add newline at end of log
        if not parts[-1].endswith("\n"):
            parts[-1] += "\n"

        return CommandLogFile._LogRecordType(*parts)


class ErrorMinifier(object):

    _FILE_RE = re.compile(r'File "(.*)"')
    _CONTINUATION_STR = "...\n"

    def __init__(self, errors_list):
        self._errors_list = errors_list
        self._capacity = None
        self._minified_error = "\n".join(self._errors_list)

    def set_capacity(self, capacity):
        logger.debug("Capacity for error string: %s", capacity)

        self._capacity = int(capacity)
        self._minified_error = self._get_minified_errors()

    def _get_minified_errors(self):  # pylint: disable=too-many-return-statements
        errors_list = self._errors_list
        errors_string = "\n".join(errors_list)
        if self._capacity is None:
            return errors_string

        if not errors_list:
            return ""

        # if within capacity return string
        if len(errors_string) <= self._capacity:
            return errors_string

        # shorten file names and try again
        for i, error in enumerate(errors_list):
            errors_list[i] = self._minify_by_shortening_file_names(error, levels=5)
        errors_string = "\n".join(errors_list)
        if len(errors_string) <= self._capacity:
            return errors_string

        # shorten file names and try again
        for i, error in enumerate(errors_list):
            errors_list[i] = self._minify_by_shortening_file_names(error, levels=4)
        errors_string = "\n".join(errors_list)
        if len(errors_string) <= self._capacity:
            return errors_string

        # return first exception if multiple exceptions occurs
        for i, error in enumerate(errors_list):
            errors_list[i] = self._minify_by_removing_nested_exceptions(error)
        errors_string = "\n".join(errors_list)
        if len(errors_string) <= self._capacity:
            return errors_string

        # last resort keep removing middle lines
        while len(errors_string) > self._capacity:
            errors_string = self._minify_by_removing_lines(errors_string)

        return errors_string

    @staticmethod
    def _minify_by_shortening_file_names(error_string, levels=5):
        new_lines = []
        for line in error_string.splitlines():
            # if original exception
            if line.strip().startswith("File") and ", line" in line:
                parts = line.split(",")
                match = ErrorMinifier._FILE_RE.search(parts[0])
                if match:
                    parts[0] = ErrorMinifier._shorten_file_name(match.group(1), levels)
                    parts[1] = parts[1].replace("line", "ln")
                line = ",".join(parts)
            # if cleaned exceptions
            elif ".py" in line and ", ln" in line:
                parts = line.split(",")
                parts[0] = ErrorMinifier._shorten_file_name(parts[0], levels)
                line = ",".join(parts)
            # remove this line
            elif "here is the traceback" in line.lower():
                continue

            new_lines.append(line)

        return "\n".join(new_lines)

    @staticmethod
    def _shorten_file_name(file_name, levels=5):
        if levels > 0:
            new_name = os.path.basename(file_name)
            file_name = os.path.dirname(file_name)
            for _ in range(levels - 1):
                new_name = os.path.join(os.path.basename(file_name), new_name)
                file_name = os.path.dirname(file_name)
            return new_name
        return file_name

    @staticmethod
    def _minify_by_removing_nested_exceptions(error_string):
        lines = error_string.splitlines()

        idx = len(lines) - 1
        for i, line in enumerate(lines):
            if "During handling of the above exception" in line:
                idx = i
                break

        # if unchanged return error_string
        if idx == len(lines) - 1:
            return error_string

        lines = lines[:idx] + [ErrorMinifier._CONTINUATION_STR] + lines[-3:]
        return "\n".join(lines)

    @staticmethod
    def _minify_by_removing_lines(error_string):
        error_string = error_string.replace(ErrorMinifier._CONTINUATION_STR, "")
        lines = error_string.splitlines()

        mid = int(len(lines) / 2) + 1
        if not (".py" in lines[mid] and ", ln" in lines[mid]):
            mid -= 1

        new_lines = []
        for i, line in enumerate(lines):
            if i == mid:
                new_lines.append(ErrorMinifier._CONTINUATION_STR.strip())
            if i in range(mid, mid + 4):
                continue
            new_lines.append(line)

        return "\n".join(new_lines)

    def __str__(self):
        if self._minified_error:
            return "```\n{}\n```".format(self._minified_error.strip())
        return ""


def _build_issue_info_tup(command_log_file=None):
    def _get_parent_proc_name():
        import psutil
        parent = psutil.Process(os.getpid()).parent()
        if parent:
            #  powershell.exe launches cmd.exe to launch the cli.
            grandparent = parent.parent()
            if grandparent and grandparent.name().lower().startswith("powershell"):
                return grandparent.name()
            # if powershell is not the grandparent, simply return the parent's name.
            return parent.name()
        return None

    format_dict = {"command_name": "", "errors_string": "",
                   "executed_command": ""}

    is_ext = False
    ext_name = None
    # Get command information, if applicable
    if command_log_file:
        command_name = command_log_file.metadata_tup.cmd
        format_dict["command_name"] = command_name

        if command_log_file.command_data_dict:
            errors_list = command_log_file.command_data_dict.get("errors", [])
            executed_command = command_log_file.command_data_dict.get("command_args", "")
            extension_name = command_log_file.command_data_dict.get("extension_name", "")
            extension_version = command_log_file.command_data_dict.get("extension_version", "")

            extension_info = ""
            if extension_name:
                extension_info = "\nExtension Name: {}. Version: {}.".format(extension_name, extension_version)
                is_ext = True
                ext_name = extension_name

            format_dict["errors_string"] = ErrorMinifier(errors_list)
            format_dict["executed_command"] = "az " + executed_command if executed_command else executed_command
            format_dict["command_name"] += extension_info

    # Get other system information
    format_dict["cli_version"] = _get_az_version_summary()
    format_dict["python_info"] = "Python {}".format(platform.python_version())
    format_dict["platform"] = "{}".format(platform.platform())
    format_dict["shell"] = "Shell: {}".format(_get_parent_proc_name())
    format_dict["auto_gen_comment"] = _AUTO_GEN_COMMENT

    pretty_url_name = _get_extension_repo_url(ext_name) if is_ext else _CLI_ISSUES_URL
    # get issue body without minification
    original_issue_body = _ISSUES_TEMPLATE.format(**format_dict)

    # First try
    capacity = _MAX_URL_LENGTH  # some browsers support a max of roughly 2000 characters
    res = _get_minified_issue_url(command_log_file, format_dict.copy(), is_ext, ext_name, capacity)
    formatted_issues_url, minified_issue_body = res
    capacity = capacity - (len(formatted_issues_url) - _MAX_URL_LENGTH)

    # while formatted issue to long, minify to new capacity
    tries = 0
    while len(formatted_issues_url) > _MAX_URL_LENGTH and tries < 25:
        # reduce capacity by difference if formatted_issues_url is too long because of url escaping
        res = _get_minified_issue_url(command_log_file, format_dict.copy(), is_ext, ext_name, capacity)
        formatted_issues_url, minified_issue_body = res
        capacity = capacity - (len(formatted_issues_url) - _MAX_URL_LENGTH)
        tries += 1

    # if something went wrong with minification (i.e. another part of the issue is unexpectedly too long)
    # then truncate the whole issue body and warn the user.
    if len(formatted_issues_url) > _MAX_URL_LENGTH:
        formatted_issues_url = formatted_issues_url[:_MAX_URL_LENGTH]
        logger.warning("Failed to properly minify issue url. "
                       "Please use 'az feedback --verbose' to get the full issue output.")

    logger.debug("Total minified issue length is %s", len(minified_issue_body))
    logger.debug("Total formatted url length is %s", len(formatted_issues_url))

    return _ISSUES_TEMPLATE_PREFIX.format(pretty_url_name), formatted_issues_url, original_issue_body, is_ext


def _get_extension_repo_url(ext_name, raw=False):
    _GITHUB_URL_STR = 'https://github.com'
    _NEW_ISSUES_STR = '/issues/new'
    try:
        project_url = resolve_project_url_from_index(extension_name=ext_name)
        if _GITHUB_URL_STR in project_url:
            return project_url.strip('/') + _NEW_ISSUES_STR
    except CLIError as ex:
        # since this is going to feedback let it fall back to the generic extensions repo
        logger.debug(ex)
    return _RAW_EXTENSIONS_ISSUES_URL if raw else _EXTENSIONS_ISSUES_URL


def _get_minified_issue_url(command_log_file, format_dict, is_ext, ext_name, capacity):
    # get issue body without errors
    minified_errors = format_dict["errors_string"]
    format_dict["errors_string"] = ""
    no_errors_issue_body = _ISSUES_TEMPLATE.format(**format_dict)

    # get minified issue body
    format_dict["errors_string"] = minified_errors
    if hasattr(minified_errors, "set_capacity"):
        logger.debug("Length of issue body before errors added: %s", len(no_errors_issue_body))
        minified_errors.set_capacity(
            capacity - len(no_errors_issue_body))  # factor in length of url and expansion of url escaped characters
    minified_issue_body = _ISSUES_TEMPLATE.format(**format_dict)

    # prefix formatted url with 'https://' if necessary and supply empty body to remove any existing issue template
    # aka.ms doesn't work well for long urls / query params
    formatted_issues_url = _get_extension_repo_url(ext_name, raw=True) if is_ext else _RAW_CLI_ISSUES_URL
    if not formatted_issues_url.startswith("http"):
        formatted_issues_url = "https://" + formatted_issues_url
    query_dict = {'body': minified_issue_body}
    if command_log_file and command_log_file.failed():
        query_dict['template'] = 'Bug_report.md'
    new_placeholder = urlencode(query_dict)
    formatted_issues_url = "{}?{}".format(formatted_issues_url, new_placeholder)

    return formatted_issues_url, minified_issue_body


def _get_az_version_summary():
    """
    This depends on get_az_version_string not being changed, add some tests to make this and other methods more robust.
    :return: az version info
    """
    az_vers_string = get_az_version_string()[0]

    lines = az_vers_string.splitlines()

    new_lines = []
    ext_line = -1
    legal_line = -1
    for i, line in enumerate(lines):
        if line.startswith("azure-cli"):
            line = " ".join(line.split())
            new_lines.append(line)
        if line.lower().startswith("extensions:"):
            ext_line = i
            continue
        l_lower = line.lower()
        if all(["legal" in l_lower, "docs" in l_lower, "info" in l_lower]):
            legal_line = i
            break

    new_lines.append("")

    if 0 < ext_line < legal_line:
        for i in range(ext_line, legal_line):
            l_lower = lines[i].lower()
            if "python location" in l_lower or "extensions directory" in l_lower:
                break

            line = " ".join(lines[i].split())
            new_lines.append(line)

    return "\n".join(new_lines)


def _get_command_log_files(cli_ctx, time_now=None):
    command_logs_dir = cli_ctx.logging.get_command_log_dir()
    files = os.listdir(command_logs_dir)
    files = (file_name for file_name in files if file_name.endswith(".log"))
    files = sorted(files)
    command_log_files = []
    for file_name in files:
        file_path = os.path.join(command_logs_dir, file_name)
        cmd_log_file = CommandLogFile(file_path, time_now)

        if cmd_log_file.metadata_tup:
            command_log_files.append(cmd_log_file)
        else:
            logger.debug("%s is an invalid command log file.", file_path)
    return command_log_files


def _display_recent_commands(cmd):
    def _pad_string(my_str, pad_len):
        while len(my_str) < pad_len:
            my_str += " "
        return my_str

    time_now = datetime.datetime.now()

    command_log_files = _get_command_log_files(cmd.cli_ctx, time_now)

    # if no command log files, return
    if not command_log_files:
        return []

    command_log_files = command_log_files[-9:]

    max_len_dict = dict(name_len=0, success_len=0, time_len=0)

    for log_file in command_log_files:
        max_len_dict["name_len"] = max(len(log_file.get_command_name_str()), max_len_dict["name_len"])
        max_len_dict["success_len"] = max(len(log_file.get_command_status()), max_len_dict["success_len"])
        max_len_dict["time_len"] = max(len(log_file.get_command_time_str()), max_len_dict["time_len"])

    print("Recent commands:\n")
    command_log_files = [None] + command_log_files
    for i, log_info in enumerate(command_log_files):
        if log_info is None:
            print("   [{}] {}".format(i, "create a generic issue."))
        else:
            cmd_name = _pad_string(log_info.get_command_name_str(), max_len_dict["name_len"])
            success_msg = _pad_string(log_info.get_command_status() + ".", max_len_dict["success_len"] + 1)
            time_msg = _pad_string(log_info.get_command_time_str() + ".", max_len_dict["time_len"] + 1)
            print("   [{}] {}: {} {}".format(i, cmd_name, success_msg, time_msg))

    return command_log_files


def _prompt_issue(recent_command_list):
    if recent_command_list:
        max_idx = len(recent_command_list) - 1
        ans = -1
        help_string = 'Please choose between 0 and {}, or enter q to quit: '.format(max_idx)

        while ans < 0 or ans > max_idx:
            try:
                ans = prompt(_MSG_CMD_ISSUE.format(max_idx), help_string=help_string)
                if ans.lower() in ["q", "quit"]:
                    ans = ans.lower()
                    break
                ans = int(ans)
            except ValueError:
                logger.warning(help_string)
                ans = -1

    else:
        ans = None
        help_string = 'Please choose between Y and N: '

        while not ans:
            ans = prompt(_MSG_ISSUE, help_string=help_string)
            if ans.lower() not in ["y", "n", "yes", "no", "q"]:
                ans = None
                continue

            # strip to short form
            ans = ans[0].lower() if ans else None

    is_ext = None

    if ans in ["y", "n"]:
        if ans == "y":
            prefix, url, original_issue, is_ext = _build_issue_info_tup()
        else:
            return False
    else:
        if ans in ["q", "quit"]:
            return False
        if ans == 0:
            prefix, url, original_issue, is_ext = _build_issue_info_tup()
        else:
            prefix, url, original_issue, is_ext = _build_issue_info_tup(recent_command_list[ans])
    print(prefix)

    # open issues page in browser and copy issue body to clipboard
    # import pyperclip
    # try:  # todo: if no longer using clipboard, remove dependency
    #     pyperclip.copy(original_issue)
    # except pyperclip.PyperclipException as ex:
    #     logger.debug(ex)
    logger.info(original_issue)

    logger.info('You can also file the issue in the Azure CLI %s repository by opening %s in the browser.',
                'Extensions' if is_ext else '',
                _EXTENSIONS_ISSUES_URL if is_ext else _CLI_ISSUES_URL)
    open_page_in_browser(url)

    return True


def handle_feedback(cmd):
    try:
        print(_MSG_INTR)
        recent_commands = _display_recent_commands(cmd)
        res = _prompt_issue(recent_commands)

        if res:
            print(_MSG_THNK)
        return
    except NoTTYException:
        raise CLIError('This command is interactive, however no tty is available.')
    except (EOFError, KeyboardInterrupt):
        print()
