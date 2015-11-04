# @author:  Renzo Frigato

import datetime
import logging

import json
import bson
import copy
import os

from ..dao import APIStorageException, containerstorage
from ..auth import containerauth, always_ok
from .. import validators
from .. import debuginfo
from .. import files
from .. import base
from .. import util


log = logging.getLogger('scitran.api')


class ContainerHandler(base.RequestHandler):
    """
    This class handle operations on a generic container

    The pattern used is:
    1) initialize request
    2) exec request
    3) check and return result

    Specific behaviors (permissions checking logic for authenticated and not superuser users, storage interaction)
    are specified in the container_handler_configurations
    """
    use_oid = {
        'groups': False,
        'projects': True,
        'sessions': True,
        'acquisitions': True
    }
    default_list_projection = ['files', 'notes', 'timestamp', 'timezone', 'public']

    container_handler_configurations = {
        'projects': {
            'storage': containerstorage.ContainerStorage('projects', use_oid=use_oid['projects']),
            'permchecker': containerauth.default_container,
            'parent_storage': containerstorage.ContainerStorage('groups', use_oid=use_oid['groups']),
            'mongo_schema_file': 'mongo/project.json',
            'payload_schema_file': 'input/project.json',
            'list_projection': {'metadata': 0},
            'children_dbc': 'sessions'
        },
        'sessions': {
            'storage': containerstorage.ContainerStorage('sessions', use_oid=use_oid['sessions']),
            'permchecker': containerauth.default_container,
            'parent_storage': containerstorage.ContainerStorage('projects', use_oid=use_oid['projects']),
            'mongo_schema_file': 'mongo/session.json',
            'payload_schema_file': 'input/session.json',
            'list_projection': {'metadata': 0},
            'children_dbc': 'acquisitions'
        },
        'acquisitions': {
            'storage': containerstorage.ContainerStorage('acquisitions', use_oid=use_oid['acquisitions']),
            'permchecker': containerauth.default_container,
            'parent_storage': containerstorage.ContainerStorage('sessions', use_oid=use_oid['sessions']),
            'mongo_schema_file': 'mongo/acquisition.json',
            'payload_schema_file': 'input/acquisition.json',
            'list_projection': {'metadata': 0}
        }
    }

    def __init__(self, request=None, response=None):
        super(ContainerHandler, self).__init__(request, response)

    def get(self, cont_name, **kwargs):
        _id = kwargs.pop('cid')
        self.config = self.container_handler_configurations[cont_name]
        self.storage = self.config['storage']
        try:
            container= self._get_container(_id)
        except APIStorageException as e:
            self.abort(400, e.message)
        permchecker = self._get_permchecker(container)
        try:
            result = permchecker(self.storage.exec_op)('GET', _id)
        except APIStorageException as e:
            self.abort(400, e.message)

        if result is None:
            self.abort(404, 'Element not found in container {} {}'.format(storage.cont_name, _id))
        if not self.superuser_request:
            self._filter_permissions(result, self.uid, self.user_site)
        if self.is_true('paths'):
            for fileinfo in result['files']:
                fileinfo['path'] = str(_id)[-3:] + '/' + str(_id) + '/' + fileinfo['filename']
        return result

    def _filter_permissions(self, result, uid, site):
        user_perm = util.user_perm(result.get('permissions', []), uid, site)
        if user_perm.get('access') != 'admin':
            result['permissions'] = [user_perm] if user_perm else []

    def get_all(self, cont_name, par_cont_name=None, par_id=None):
        self.config = self.container_handler_configurations[cont_name]
        self.storage = self.config['storage']
        projection = self.config['list_projection']
        if self.superuser_request:
            permchecker = always_ok
        elif self.public_request:
            permchecker = containerauth.list_public_request
        else:
            admin_only = self.is_true('admin')
            permchecker = containerauth.list_permission_checker(self, admin_only)
        if par_cont_name:
            if not par_id:
                self.abort(500, 'par_id is required when par_cont_name is provided')
            if self.use_oid.get(par_cont_name):
                if not bson.ObjectId.is_valid(par_id):
                    self.abort(400, 'not a valid object id')
                par_id = bson.ObjectId(par_id)
            query = {par_cont_name[:-1]: par_id}
        else:
            query = {}
        results = permchecker(self.storage.exec_op)('GET', query=query, public=self.public_request, projection=projection)
        if results is None:
            self.abort(404, 'Element not found in container {} {}'.format(storage.cont_name, _id))
        self._filter_all_permissions(results, self.uid, self.user_site)
        if self.is_true('counts'):
            self._add_results_counts(results, cont_name)
        if cont_name == 'sessions' and self.is_true('measurements'):
            self._add_session_measurements(results)
        if self.debug:
            debuginfo.add_debuginfo(self, cont_name, results)
        return results

    def _filter_all_permissions(self, results, uid, site):
        for result in results:
            user_perm = util.user_perm(result.get('permissions', []), uid, site)
            result['permissions'] = [user_perm] if user_perm else []
        return results

    def _add_results_counts(self, results):
        dbc_name = self.config.get('children_dbc')
        el_cont_name = cont_name[:-1]
        dbc = self.app.db.get(dbc_name)
        counts =  dbc.aggregate([
            {'$match': {el_cont_name: {'$in': [proj['_id'] for proj in projects]}}},
            {'$group': {'_id': '$' + el_cont_name, 'count': {"$sum": 1}}}
            ])
        counts = {elem['_id']: elem['count'] for elem in counts}
        for elem in results:
            elem[dbc_name[:-1] + '_count'] = counts.get(elem['_id'], 0)

    def _add_session_measurements(self, results):
        session_measurements = {}
        session_measurements = self.app.db.acquisitions.aggregate([
            {'$match': {'session': {'$in': [sess['_id'] for sess in results]}}},
            {'$group': {'_id': '$session', 'measurements': {'$addToSet': '$datatype'}}}
            ])
        session_measurements = {sess['_id']: sess['measurements'] for sess in session_measurements}
        for sess in results:
            sess['measurements'] = session_measurements.get(sess['_id'], None)

    def get_all_for_user(self, cont_name, uid):
        self.config = self.container_handler_configurations[cont_name]
        self.storage = self.config['storage']
        projection = self.config['list_projection']
        if self.superuser_request:
            permchecker = always_ok
        elif self.public_request:
            self.abort(403, 'this request is not allowed')
        else:
            permchecker = containerauth.list_permission_checker(self)
        query = {}
        user = {
            '_id': uid,
            'site': self.app.config['site_id']
        }
        try:
            results = permchecker(self.storage.exec_op)('GET', query=query, user=user, projection=projection)
        except APIStorageException as e:
            self.abort(400, e.message)
        if results is None:
            self.abort(404, 'Element not found in container {} {}'.format(storage.cont_name, _id))
        self._filter_all_permissions(results, self.uid, self.user_site)
        if self.debug:
            debuginfo.add_debuginfo(self, cont_name, results)
        return results

    def post(self, cont_name, **kwargs):
        self.config = self.container_handler_configurations[cont_name]
        self.storage = self.config['storage']
        mongo_validator, payload_validator = self._get_validators()

        payload = self.request.json_body
        log.debug(payload)
        payload_validator(payload, 'POST')
        parent_container, parent_id_property = self._get_parent_container(payload)
        if cont_name == 'sessions':
            payload['group'] = parent_container['group']
        payload[parent_id_property] = parent_container['_id']
        if self.is_true('inherit') and cont_name == 'projects':
            payload['permissions'] = parent_container.get('roles')
        elif cont_name =='projects':
            payload['permissions'] = [{'_id': self.uid, 'access': 'admin', 'site': self.user_site}]
        else:
            payload['permissions'] = parent_container.get('permissions', [])
        payload['created'] = payload['modified'] = datetime.datetime.utcnow()
        if payload.get('timestamp'):
            payload['timestamp'] = dateutil.parser.parse(payload['timestamp'])
        permchecker = self._get_permchecker(parent_container=parent_container)
        result = mongo_validator(permchecker(self.storage.exec_op))('POST', payload=payload)
        if result.acknowledged:
            return {'_id': result.inserted_id}
        else:
            self.abort(404, 'Element not added in container {} {}'.format(storage.cont_name, _id))

    def put(self, cont_name, **kwargs):
        _id = kwargs.pop('cid')
        self.config = self.container_handler_configurations[cont_name]
        self.storage = self.config['storage']
        container = self._get_container(_id)
        mongo_validator, payload_validator = self._get_validators()

        payload = self.request.json_body
        payload_validator(payload, 'PUT')

        target_parent_container, parent_id_property = self._get_parent_container(payload)
        if target_parent_container:
            payload[parent_id_property] = target_parent_container['_id']
            if cont_name == 'sessions':
                payload['group'] = target_parent_container['group']
            payload['permissions'] = target_parent_container.get('roles')
            if payload['permissions'] is None:
                payload['permissions'] = target_parent_container['permissions']

        permchecker = self._get_permchecker(container, target_parent_container)
        payload['modified'] = datetime.datetime.utcnow()
        if payload.get('timestamp'):
            payload['timestamp'] = dateutil.parser.parse(payload['timestamp'])
        try:
            result = mongo_validator(permchecker(self.storage.exec_op))('PUT', _id=_id, payload=payload)
        except APIStorageException as e:
            self.abort(400, e.message)

        if result.modified_count == 1:
            return {'modified': result.modified_count}
        else:
            self.abort(404, 'Element not updated in container {} {}'.format(storage.cont_name, _id))

    def delete(self, cont_name, **kwargs):
        _id = kwargs.pop('cid')
        self.config = self.container_handler_configurations[cont_name]
        self.storage = self.config['storage']
        container= self._get_container(_id)
        target_parent_container, parent_id_property = self._get_parent_container(container)
        permchecker = self._get_permchecker(container, target_parent_container)
        try:
            result = permchecker(self.storage.exec_op)('DELETE', _id)
        except APIStorageException as e:
            self.abort(400, e.message)

        if result.deleted_count == 1:
            return {'deleted': result.deleted_count}
        else:
            self.abort(404, 'Element not removed from container {} {}'.format(storage.cont_name, _id))

    def get_groups_with_project(self):
        group_ids = list(set((p['group'] for p in self.get_all('projects'))))
        return list(self.app.db.groups.find({'_id': {'$in': group_ids}}, ['name']))


    def _get_validators(self):
        mongo_validator = validators.mongo_from_schema_file(self, self.config.get('mongo_schema_file'))
        payload_validator = validators.payload_from_schema_file(self, self.config.get('payload_schema_file'))
        return mongo_validator, payload_validator

    def _get_parent_container(self, payload):
        if not self.config.get('parent_storage'):
            return None, None
        log.debug(payload)
        parent_storage = self.config['parent_storage']
        parent_id_property = parent_storage.cont_name[:-1]
        log.debug(parent_id_property)
        parent_id = payload.get(parent_id_property)
        log.debug(parent_id)
        if parent_id:
            parent_storage.dbc = self.app.db[parent_storage.cont_name]
            parent_container = parent_storage.get_container(parent_id)
            if parent_container is None:
                self.abort(404, 'Element {} not found in container {}'.format(parent_id, parent_storage.cont_name))
        else:
            parent_container = None
        log.debug(parent_container)
        return parent_container, parent_id_property


    def _get_container(self, _id):
        container = self.storage.get_container(_id)
        if container is not None:
            return container
        else:
            self.abort(404, 'Element {} not found in container {}'.format(_id, self.storage.cont_name))

    def _get_permchecker(self, container=None, parent_container=None):
        if self.superuser_request:
            return always_ok
        elif self.public_request:
            return containerauth.public_request(self, container, parent_container)
        else:
            permchecker = self.config['permchecker']
            return permchecker(self, container, parent_container)
