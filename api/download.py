import json
import pytz
import os.path
import tarfile
import datetime
import cStringIO
import urllib

from . import base
from . import validators

from . import util
from . import config

log = config.log


def _filter_check(property_filter, property_values):
    minus = set(property_filter.get('-', []))
    plus = set(property_filter.get('+', []))
    if not minus.isdisjoint(property_values):
        return False
    if plus and plus.isdisjoint(property_values):
        return False
    return True


def _append_targets(targets, container, prefix, total_size, total_cnt, optional, data_path, filters):
    for f in container.get('files', []):
        if filters:
            filtered = True
            for filter_ in filters:
                type_as_list = [f['type']] if f.get('type') else []
                if (
                    _filter_check(filter_.get('tags', {}), f.get('tags', [])) and
                    _filter_check(filter_.get('types', {}), type_as_list)
                    ):
                    filtered = False
                    break
            if filtered:
                continue
        if optional or not f.get('optional', False):
            filepath = os.path.join(data_path, util.path_from_hash(f['hash']))
            if os.path.exists(filepath): # silently skip missing files
                targets.append((filepath, prefix + '/' + urllib.url2pathname(f['name']), f['size']))
                total_size += f['size']
                total_cnt += 1
    return total_size, total_cnt


class Download(base.RequestHandler):

    def _preflight_archivestream(self, req_spec, snapshot=False):
        session_cont_name = 'session_snapshots' if snapshot else 'sessions'
        acquisition_cont_name = 'acquisition_snapshots' if snapshot else 'acquisitions'
        project_cont_name = 'project_snapshots' if snapshot else 'projects'
        data_path = config.get_item('persistent', 'data_path')
        arc_prefix = 'sdm'
        file_cnt = 0
        total_size = 0
        targets = []
        # FIXME: check permissions of everything
        used_subpaths = {}
        for item in req_spec['nodes']:
            item_id = util.ObjectId(item['_id'])
            if item['level'] == 'project':
                project = config.db[project_cont_name].find_one({'_id': item_id}, ['group', 'label', 'files'])
                prefix = '/'.join([arc_prefix, project['group'], project['label']])
                total_size, file_cnt = _append_targets(targets, project, prefix, total_size, file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
                sessions = config.db[session_cont_name].find({'project': item_id}, ['label', 'files', 'uid', 'timestamp', 'timezone'])
                session_dict = {session['_id']: session for session in sessions}
                acquisitions = config.db[acquisition_cont_name].find({'session': {'$in': session_dict.keys()}}, ['label', 'files', 'session', 'uid', 'timestamp', 'timezone'])
                session_prefixes = {}
                for session in session_dict.itervalues():
                    session_prefix = prefix + '/' + self._path_from_container(session, used_subpaths, project['_id'])
                    session_prefixes[session['_id']] = session_prefix
                    total_size, file_cnt = _append_targets(targets, session, session_prefix, total_size, file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
                for acq in acquisitions:
                    session = session_dict[acq['session']]
                    acq_prefix = session_prefixes[session['_id']] + '/' + self._path_from_container(acq, used_subpaths, session['_id'])
                    total_size, file_cnt = _append_targets(targets, acq, acq_prefix, total_size, file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
            elif item['level'] == 'session':
                session = config.db[session_cont_name].find_one({'_id': item_id}, ['project', 'label', 'files', 'uid', 'timestamp', 'timezone'])
                project = config.db[project_cont_name].find_one({'_id': session['project']}, ['group', 'label'])
                prefix = project['group'] + '/' + project['label'] + '/' + self._path_from_container(session, used_subpaths, project['_id'])
                total_size, file_cnt = _append_targets(targets, session, prefix, total_size, file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
                acquisitions = config.db[acquisition_cont_name].find({'session': item_id}, ['label', 'files', 'uid', 'timestamp', 'timezone'])
                for acq in acquisitions:
                    acq_prefix = prefix + '/' + self._path_from_container(acq, used_subpaths, session['_id'])
                    total_size, file_cnt = _append_targets(targets, acq, acq_prefix, total_size, file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
            elif item['level'] == 'acquisition':
                acq = config.db[acquisition_cont_name].find_one({'_id': item_id}, ['session', 'label', 'files', 'uid', 'timestamp', 'timezone'])
                session = config.db[session_cont_name].find_one({'_id': acq['session']}, ['project', 'label', 'uid', 'timestamp', 'timezone'])
                project = config.db[project_cont_name].find_one({'_id': session['project']}, ['group', 'label'])
                prefix = project['group'] + '/' + project['label'] + '/' + self._path_from_container(session, used_subpaths, project['_id']) + '/' + self._path_from_container(acq, used_subpaths, session['_id'])
                total_size, file_cnt = _append_targets(targets, acq, prefix, total_size, file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
        log.debug(json.dumps(targets, sort_keys=True, indent=4, separators=(',', ': ')))
        filename = 'sdm_' + datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S') + '.tar'
        ticket = util.download_ticket(self.request.client_addr, 'batch', targets, filename, total_size)
        config.db.downloads.insert_one(ticket)
        return {'ticket': ticket['_id'], 'file_cnt': file_cnt, 'size': total_size}

    def _path_from_container(self, container, used_subpaths, parent_id):
        def _find_new_path(path, list_used_subpaths):
            """from the input path finds a path that hasn't been used"""
            if path not in list_used_subpaths:
                return path
            i = 0
            while True:
                modified_path = path + '_' + str(i)
                if modified_path not in list_used_subpaths:
                    return modified_path
                i += 1
        path = None
        if not path and container.get('label'):
            path = container['label']
        if not path and container.get('timestamp'):
            timezone = container.get('timezone')
            if timezone:
                path = pytz.timezone('UTC').localize(container['timestamp']).astimezone(pytz.timezone(timezone)).strftime('%Y%m%d_%H%M')
            else:
                path = container['timestamp'].strftime('%Y%m%d_%H%M')
        if not path and container.get('uid'):
            path = container['uid']
        if not path:
            path = 'untitled'
        path = _find_new_path(path, used_subpaths.get(parent_id, []))
        used_subpaths[parent_id] = used_subpaths.get(parent_id, []) + [path]
        return path

    def _preflight_archivestream_bids(self, req_spec, snapshot=False):
        session_cont_name = 'session_snapshots' if snapshot else 'sessions'
        acquisition_cont_name = 'acquisition_snapshots' if snapshot else 'acquisitions'
        project_cont_name = 'project_snapshots' if snapshot else 'projects'
        data_path = config.get_item('persistent', 'data_path')
        file_cnt = 0
        total_size = 0
        targets = []
        # FIXME: check permissions of everything
        projects = []
        prefix = 'untitled'
        used_subpaths = {}
        if len(req_spec['nodes']) != 1:
            self.abort(400, 'bids downloads are limited to single dataset downloads')
        for item in req_spec['nodes']:
            item_id = util.ObjectId(item['_id'])
            if item['level'] == 'project':
                project = config.db[project_cont_name].find_one({'_id': item_id}, ['group', 'label', 'files', 'notes'])
                projects.append(item_id)
                prefix = project['label']
                total_size, file_cnt = _append_targets(targets, project, prefix, total_size,
                                                       file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
                ses_or_subj_list = config.db[session_cont_name].find({'project': item_id}, ['_id', 'label', 'files', 'subject.code', 'subject_code', 'uid', 'timestamp', 'timezone'])
                subject_prefixes = {
                    'missing_subject': prefix + '/missing_subject'
                }
                sessions = {}
                for ses_or_subj in ses_or_subj_list:
                    subj_code = ses_or_subj.get('subject', {}).get('code') or ses_or_subj.get('subject_code')
                    if subj_code == 'subject':
                        subject_prefix = prefix + '/' + self._path_from_container(ses_or_subj, used_subpaths, project['_id'])
                        total_size, file_cnt = _append_targets(targets, ses_or_subj, subject_prefix, total_size,
                                                               file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
                        subject_prefixes[str(ses_or_subj.get('_id'))] = subject_prefix
                    elif subj_code:
                        sessions[subj_code] = sessions.get(subj_code, []) + [ses_or_subj]
                    else:
                        sessions['missing_subject'] = sessions.get('missing_subject', []) + [ses_or_subj]
                for subj_code, ses_list in sessions.items():
                    subject_prefix = subject_prefixes.get(subj_code)
                    if not subject_prefix:
                        continue
                    for session in ses_list:
                        session_prefix = subject_prefix + '/' + self._path_from_container(session, used_subpaths, subj_code)
                        total_size, file_cnt = _append_targets(targets, session, session_prefix, total_size,
                                                               file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
                        acquisitions = config.db[acquisition_cont_name].find({'session': session['_id']}, ['label', 'files', 'uid', 'timestamp', 'timezone'])
                        for acq in acquisitions:
                            acq_prefix = session_prefix + '/' + self._path_from_container(acq, used_subpaths, session['_id'])
                            total_size, file_cnt = _append_targets(targets, acq, acq_prefix, total_size,
                                                                   file_cnt, req_spec['optional'], data_path, req_spec.get('filters'))
        log.debug(json.dumps(targets, sort_keys=True, indent=4, separators=(',', ': ')))
        filename = prefix.replace(',', '') + '_' + datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S') + '.tar'
        ticket = util.download_ticket(self.request.client_addr, 'batch', targets, filename, total_size, projects)
        config.db.downloads.insert_one(ticket)
        return {'ticket': ticket['_id'], 'file_cnt': file_cnt, 'size': total_size}

    def _archivestream(self, ticket):
        BLOCKSIZE = 512
        CHUNKSIZE = 2**20  # stream files in 1MB chunks
        stream = cStringIO.StringIO()
        with tarfile.open(mode='w|', fileobj=stream) as archive:
            for filepath, arcpath, _ in ticket['target']:
                yield archive.gettarinfo(filepath, arcpath).tobuf()
                with open(filepath, 'rb') as fd:
                    for chunk in iter(lambda: fd.read(CHUNKSIZE), ''):
                        yield chunk
                    if len(chunk) % BLOCKSIZE != 0:
                        yield (BLOCKSIZE - (len(chunk) % BLOCKSIZE)) * b'\0'
        yield stream.getvalue() # get tar stream trailer
        stream.close()

    def _symlinkarchivestream(self, ticket, data_path):
        for filepath, arcpath, _ in ticket['target']:
            t = tarfile.TarInfo(name=arcpath)
            t.type = tarfile.SYMTYPE
            t.linkname = os.path.relpath(filepath, data_path)
            yield t.tobuf()
        stream = cStringIO.StringIO()
        with tarfile.open(mode='w|', fileobj=stream) as archive:
            pass
        yield stream.getvalue() # get tar stream trailer
        stream.close()

    def download_snapshot(self):
        return self.download(snapshot=True)

    def download(self, snapshot=False):
        """
        In downloads we use filters in the payload to exclude/include files.
        To pass a single filter, each of its conditions should be satisfied.
        If a file pass at least one filter, it is included in the targets.
        For example:

        download_payload = {
            'optional': True,
            'nodes': [{'level':'project', '_id':project_id}],
            'filters':[{
                'tags':{'+':['incomplete']}
            },
            {
                'types':{'-':['dicom']}
            }]
        }
        will download files with tag 'incomplete' OR type different from 'dicom'

        download_payload = {
            'optional': True,
            'nodes': [{'level':'project', '_id':project_id}],
            'filters':[{
                'tags':{'+':['incomplete']},
                'types':{'+':['dicom']}
            }]
        }
        will download only files with tag 'incomplete' AND type different from 'dicom'
        """
        ticket_id = self.get_param('ticket')
        if ticket_id:
            ticket = config.db.downloads.find_one({'_id': ticket_id})
            if not ticket:
                self.abort(404, 'no such ticket')
            if ticket['ip'] != self.request.client_addr:
                self.abort(400, 'ticket not for this source IP')
            if self.get_param('symlinks'):
                self.response.app_iter = self._symlinkarchivestream(ticket, config.get_item('persistent', 'data_path'))
            else:
                self.response.app_iter = self._archivestream(ticket)
            self.response.headers['Content-Type'] = 'application/octet-stream'
            self.response.headers['Content-Disposition'] = 'attachment; filename=' + str(ticket['filename'])
            for project_id in ticket['projects']:
                if snapshot:
                    config.db.project_snapshots.update_one({'_id': project_id}, {'$inc': {'counter': 1}})
                else:
                    config.db.projects.update_one({'_id': project_id}, {'$inc': {'counter': 1}})
        else:
            req_spec = self.request.json_body
            validator = validators.payload_from_schema_file(self, 'download.json')
            validator(req_spec, 'POST')
            log.debug(json.dumps(req_spec, sort_keys=True, indent=4, separators=(',', ': ')))
            if self.get_param('format') == 'bids':
                return self._preflight_archivestream_bids(req_spec, snapshot=snapshot)
            else:
                return self._preflight_archivestream(req_spec, snapshot=snapshot)

