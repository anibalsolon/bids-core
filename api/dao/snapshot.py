from .. import config
from .. import util

from pymongo import ReturnDocument

log = config.log

def _new_version(project_id):
    project_id = util.ObjectId(project_id)
    project = config.db.projects.find_one_and_update(
        {'_id': project_id},
        {'$inc': {'snapshot_version': 1}},
        return_document=ReturnDocument.AFTER
    )
    version = {
        'snapshot': project
    }
    sessions = list(config.db.sessions.find({'project': project_id}, {'project': 0, 'permissions': 0}))
    for session in sessions:
        acquisitions = list(config.db.acquisitions.find({'session': session['_id']}, {'session': 0, 'permissions': 0}))
        session['permissions'] = project['permissions']
        for a in acquisitions:
            a['permissions'] = project['permissions']
        version[session['_id']] = acquisitions
    version[project_id] = sessions
    return version


def _store(hierarchy, snap_id=None):
    project = hierarchy['snapshot']
    project['original'] = project.pop('_id')
    if snap_id:
        project['_id'] = util.ObjectId(snap_id)
    result = config.db.project_snapshots.insert_one(project)
    project_id = result.inserted_id
    subjects = []
    sessions = {}
    for session in hierarchy[project['original']]:
        session['project'] = project_id
        session['original'] = session.pop('_id')
        subject_code = session.get('subject', {}).get('code', '')
        if subject_code == 'subject':
            subjects.append(session)
        else:
            sessions[subject_code] = sessions.get(subject_code, [])
            sessions[subject_code].append(session)
    sessions_list = []
    if subjects:
        new_subject_ids = config.db.session_snapshots.insert_many(subjects).inserted_ids
        for i, subject in enumerate(subjects):
            new_sub_id = new_subject_ids[i]
            for s in sessions.get(str(subject['original']), []):
                s['subject']['code'] = str(new_sub_id)
                sessions_list.append(s)
    else:
        session_list = hierarchy[project['original']]
    if not sessions_list:
        return result
    session_ids = config.db.session_snapshots.insert_many(sessions_list).inserted_ids
    acquisitions = []
    for i, session in enumerate(sessions_list):
        session_id = session_ids[i]
        for acquisition in hierarchy[session['original']]:
            acquisition['session'] = session_id
            acquisition['original'] = acquisition.pop('_id')
            acquisitions.append(acquisition)
    if acquisitions:
        config.db.acquisition_snapshots.insert_many(acquisitions)
    return result


def create(method, _id, payload=None):
    hierarchy = _new_version(_id)
    if payload:
        snap_id = payload['_id']
    else:
        snap_id = None
    return _store(hierarchy, snap_id)


def remove(method, _id, payload=None):
    snapshot_id = util.ObjectId(_id)
    result = config.db.project_snapshots.find_one_and_delete({'_id': snapshot_id})
    session_snapshot_ids = [s['_id'] for s in config.db.session_snapshots.find({'project': snapshot_id})]
    config.db.session_snapshots.delete_many({'_id': {'$in': session_snapshot_ids}})
    config.db.acquisition_snapshots.delete_many({'session': {'$in': session_snapshot_ids}})
    return result

def remove_private_snapshots_for_project(pid):
    pid = util.ObjectId(pid)
    project_snapshot_ids = [sn['_id'] for sn in config.db.project_snapshots.find({'original': pid, 'public': False})]
    result = config.db.project_snapshots.delete_many({'original': pid, 'public': False})
    session_snapshot_ids = [s['_id'] for s in config.db.session_snapshots.find({'project': {'$in': project_snapshot_ids}})]
    config.db.session_snapshots.delete_many({'_id': {'$in': session_snapshot_ids}})
    config.db.acquisition_snapshots.delete_many({'session': {'$in': session_snapshot_ids}})
    return result

def remove_permissions_from_snapshots(pid):
    pid = util.ObjectId(pid)
    project_snapshot_ids = [sn['_id'] for sn in config.db.project_snapshots.find({'original': pid})]
    result = config.db.project_snapshots.update_many({'original': pid}, {'$set':{'permissions': []}})
    session_snapshot_ids = [s['_id'] for s in config.db.session_snapshots.find({'project': {'$in': project_snapshot_ids}})]
    config.db.session_snapshots.update_many({'_id': {'$in': session_snapshot_ids}}, {'$set':{'permissions': []}})
    config.db.acquisition_snapshots.update_many({'session': {'$in': session_snapshot_ids}}, {'$set':{'permissions': []}})
    return result

def make_public(method, _id, payload=None):
    public = payload['value']
    snapshot_id = util.ObjectId(_id)
    result = config.db.project_snapshots.find_one_and_update({'_id': snapshot_id}, {'$set':{'public': public}})
    session_snapshot_ids = [s['_id'] for s in config.db.session_snapshots.find({'project': snapshot_id})]
    config.db.session_snapshots.update_many({'_id': {'$in': session_snapshot_ids}}, {'$set':{'public': public}})
    config.db.acquisition_snapshots.update_many({'session': {'$in': session_snapshot_ids}}, {'$set':{'public': public}})
    return result
