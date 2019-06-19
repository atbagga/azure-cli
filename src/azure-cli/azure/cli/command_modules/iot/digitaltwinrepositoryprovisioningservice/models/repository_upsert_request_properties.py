# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
# pylint: skip-file
# coding=utf-8
# --------------------------------------------------------------------------
# Code generated by Microsoft (R) AutoRest Code Generator.
# Changes may cause incorrect behavior and will be lost if the code is
# regenerated.
# --------------------------------------------------------------------------

from msrest.serialization import Model


class RepositoryUpsertRequestProperties(Model):
    """Repository Upsert Request properties.

    :param id: Gets or sets repository id. NULL in case of repository create.
    :type id: str
    :param name: Gets or sets repository name.
    :type name: str
    """

    _attribute_map = {
        'id': {'key': 'id', 'type': 'str'},
        'name': {'key': 'name', 'type': 'str'},
    }

    def __init__(self, id=None, name=None):
        super(RepositoryUpsertRequestProperties, self).__init__()
        self.id = id
        self.name = name
