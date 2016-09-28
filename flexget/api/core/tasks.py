from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import argparse
import cgi
import copy
from datetime import datetime
from json import JSONEncoder

from flask import jsonify, Response, request
from queue import Queue, Empty

from flexget.api import api, APIResource
from flexget.api.app import APIError, NotFoundError, CannotAddResource, BadRequest, success_response, \
    base_message_schema, etag
from flexget.config_schema import process_config
from flexget.entry import Entry
from flexget.event import event
from flexget.options import get_parser
from flexget.task import task_phases
from flexget.utils import json
from flexget.utils import requests
from flexget.utils.lazy_dict import LazyLookup

# Tasks API
tasks_api = api.namespace('tasks', description='Manage Tasks')


class ObjectsContainer(object):
    tasks_list_object = {
        "type": "array",
        "items": {'$ref': '#/definitions/tasks.task'}
    }

    task_input_object = {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'config': {'$ref': '/schema/plugins'}
        },
        'required': ['name', 'config'],
        'additionalProperties': False
    }

    task_return_object = copy.deepcopy(task_input_object)
    task_return_object['properties']['config'] = {'type': 'object'}

    task_queue_schema = {
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'name': {'type': 'string'},
                'current_phase': {'type': 'string'},
                'current_plugin': {'type': 'string'},
            }}
    }

    task_execution_results_schema = {
        'type': 'object',
        'properties': {
            'task': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'name': {'type': 'string'},
                    'stream': {
                        'type': 'array',
                        'items': {
                            'progress': {
                                'type': 'object',
                                'properties': {
                                    'status': {'type': 'string', 'enum': ['pending', 'running', 'complete']},
                                    'phase': {'type': 'string', 'enum': task_phases},
                                    'plugin': {'type': 'string'},
                                    'percent': {'type': 'float'}
                                }
                            },
                            'summary': {
                                'type': 'object',
                                'properties': {
                                    'accepted': {'type': 'integer'},
                                    'rejected': {'type': 'integer'},
                                    'failed': {'type': 'integer'},
                                    'undecided': {'type': 'integer'},
                                    'aborted': {'type': 'boolean'},
                                    'abort_reason': {'type': 'string'},
                                }
                            },
                            'entry_dump': {'type': 'array', 'items': {'type': 'object'}},
                            'log': {'type': 'string'}
                        }
                    }
                }
            }
        }
    }

    inject_input = {
        'type': 'object',
        'properties': {
            'title': {'type': 'string'},
            'url': {'type': 'string', 'format': 'url'},
            'force': {'type': 'boolean'},
            'accept': {'type': 'boolean'},
            'fields': {'type': 'object'}
        },
        'required': ['url']
    }

    task_execution_input = {
        'type': 'object',
        'properties': {
            'tasks': {'type': "array",
                      'items': {'type': 'string'},
                      'minItems': 1,
                      'uniqueItems': True},
            'progress': {'type': 'boolean', 'default': True},
            'summary': {'type': 'boolean', 'default': True},
            'entry_dump': {'type': 'boolean', 'default': True},
            'inject': {'type': 'array',
                       'items': inject_input},
            'loglevel': {'type': "string",
                         "enum": ['critical', 'error', 'warning', 'info', 'verbose', 'debug', 'trace']}
        },
        'required': ['tasks']

    }


tasks_list_schema = api.schema('tasks.list', ObjectsContainer.tasks_list_object)
task_input_schema = api.schema('tasks.task', ObjectsContainer.task_input_object)
task_return_schema = api.schema('tasks.task', ObjectsContainer.task_return_object)
task_api_queue_schema = api.schema('task.queue', ObjectsContainer.task_queue_schema)
task_api_execute_schema = api.schema('task.execution', ObjectsContainer.task_execution_results_schema)
task_execution_schema = api.schema('task_execution_input', ObjectsContainer.task_execution_input)

task_api_desc = 'Task config schema too large to display, you can view the schema using the schema API'


@tasks_api.route('/')
@api.doc(description=task_api_desc)
class TasksAPI(APIResource):
    @etag
    @api.response(200, model=tasks_list_schema)
    def get(self, session=None):
        """ List all tasks """
        tasks = []
        for name, config in self.manager.user_config.get('tasks', {}).items():
            tasks.append({'name': name, 'config': config})
        return jsonify(tasks)

    @api.validate(task_input_schema, description='New task object')
    @api.response(201, description='Newly created task', model=task_return_schema)
    @api.response(CannotAddResource)
    @api.response(APIError)
    def post(self, session=None):
        """ Add new task """
        data = request.json

        task_name = data['name']

        if task_name in self.manager.user_config.get('tasks', {}):
            raise CannotAddResource('task already exists')

        if 'tasks' not in self.manager.user_config:
            self.manager.user_config['tasks'] = {}
        if 'tasks' not in self.manager.config:
            self.manager.config['tasks'] = {}

        task_schema_processed = copy.deepcopy(data)
        errors = process_config(task_schema_processed, schema=task_input_schema.__schema__, set_defaults=True)

        if errors:
            raise APIError('problem loading config, raise a BUG as this should not happen!')

        self.manager.user_config['tasks'][task_name] = data['config']
        self.manager.config['tasks'][task_name] = task_schema_processed['config']

        self.manager.save_config()
        self.manager.config_changed()
        rsp = jsonify({'name': task_name, 'config': self.manager.user_config['tasks'][task_name]})
        rsp.status_code = 201
        return rsp


@tasks_api.route('/<task>/')
@api.doc(params={'task': 'task name'}, description=task_api_desc)
@api.response(APIError, description='unable to read config')
class TaskAPI(APIResource):
    @etag
    @api.response(200, model=task_return_schema)
    @api.response(NotFoundError, description='task not found')
    def get(self, task, session=None):
        """ Get task config """
        if task not in self.manager.user_config.get('tasks', {}):
            raise NotFoundError('task `%s` not found' % task)

        return jsonify({'name': task, 'config': self.manager.user_config['tasks'][task]})

    @api.validate(task_input_schema)
    @api.response(200, model=task_return_schema)
    @api.response(NotFoundError)
    @api.response(BadRequest)
    def put(self, task, session=None):
        """ Update tasks config """
        data = request.json

        new_task_name = data['name']

        if task not in self.manager.user_config.get('tasks', {}):
            raise NotFoundError('task `%s` not found' % task)

        if 'tasks' not in self.manager.user_config:
            self.manager.user_config['tasks'] = {}
        if 'tasks' not in self.manager.config:
            self.manager.config['tasks'] = {}

        if task != new_task_name:
            # Rename task
            if new_task_name in self.manager.user_config['tasks']:
                raise BadRequest('cannot rename task as it already exist')

            del self.manager.user_config['tasks'][task]
            del self.manager.config['tasks'][task]

        # Process the task config
        task_schema_processed = copy.deepcopy(data)
        errors = process_config(task_schema_processed, schema=task_return_schema.__schema__, set_defaults=True)

        if errors:
            raise APIError('problem loading config, raise a BUG as this should not happen!')

        self.manager.user_config['tasks'][new_task_name] = data['config']
        self.manager.config['tasks'][new_task_name] = task_schema_processed['config']

        self.manager.save_config()
        self.manager.config_changed()

        rsp = jsonify({'name': new_task_name, 'config': self.manager.user_config['tasks'][new_task_name]})
        rsp.status_code = 200
        return rsp

    @api.response(200, model=base_message_schema, description='deleted task')
    @api.response(NotFoundError)
    def delete(self, task, session=None):
        """ Delete a task """
        try:
            self.manager.config['tasks'].pop(task)
            self.manager.user_config['tasks'].pop(task)
        except KeyError:
            raise NotFoundError('task does not exist')

        self.manager.save_config()
        self.manager.config_changed()
        return success_response('successfully deleted task')


def _task_info_dict(task):
    return {
        'id': int(task.id),
        'name': task.name,
        'current_phase': task.current_phase,
        'current_plugin': task.current_plugin,
    }


@tasks_api.route('/queue/')
class TaskQueueAPI(APIResource):
    @api.response(200, model=task_api_queue_schema)
    def get(self, session=None):
        """ List task(s) in queue for execution """
        tasks = [_task_info_dict(task) for task in self.manager.task_queue.run_queue.queue]

        if self.manager.task_queue.current_task:
            tasks.insert(0, _task_info_dict(self.manager.task_queue.current_task))

        return jsonify(tasks)


class ExecuteLog(Queue):
    """ Supports task log streaming by acting like a file object """

    def write(self, s):
        self.put(json.dumps({'log': s}))


_streams = {}

execution_doc = "'tasks': Array of tasks to be execute. Required<br>" \
                "'progress': Include task progress updates<br>" \
                "'summary': Include task summary<br>" \
                "'entry_dump': Include dump of entries including fields<br>" \
                "'inject': A List of entry objects. See payload description for additional information<br>" \
                "'loglevel': Specify log level. One of 'critical', 'error', 'warning', 'info', 'verbose', " \
                "'debug', 'trace'. Default is 'none'<br>"

entry_doc = "Entry object:<br>" \
            "'title': Title of the entry. If not supplied it will be attempted to retrieve it from URL headers<br>" \
            "'url': URL of the entry, mandatory<br>" \
            "'accept': Accept this entry immediately upon injection<br>" \
            "'force': Prevent any plugins from rejecting this entry<br>" \
            "'fields': A list of objects that can contain any other value for the entry"

inject_api = api.namespace('inject', description='Entry injection API')


@inject_api.route('/')
@tasks_api.route('/execute/')
@api.doc(description=execution_doc)
class TaskExecutionAPI(APIResource):
    @api.response(NotFoundError)
    @api.response(BadRequest)
    @api.response(200, model=task_api_execute_schema)
    @api.validate(task_execution_schema, description=entry_doc)
    def post(self, session=None):
        """ Execute task and stream results """
        data = request.json
        for task in data.get('tasks'):
            if task.lower() not in [t.lower() for t in self.manager.user_config.get('tasks', {}).keys()]:
                raise NotFoundError('task %s does not exist' % task)

        queue = ExecuteLog()
        output = queue if data.get('loglevel') else None
        stream = True if any(
            arg[0] in ['progress', 'summary', 'loglevel', 'entry_dump'] for arg in data.items() if arg[1]) else False
        loglevel = data.pop('loglevel', None)

        # This emulates the CLI command of using `--now` and `no-cache`
        options = {'interval_ignore': data.pop('now', None),
                   'nocache': data.pop('no_cache', None),
                   'allow_manual': True}

        for option, value in data.items():
            options[option] = value

        if data.get('inject'):
            entries = []
            for item in data.get('inject'):
                entry = Entry()
                entry['url'] = item['url']
                if not item.get('title'):
                    try:
                        value, params = cgi.parse_header(requests.head(item['url']).headers['Content-Disposition'])
                        entry['title'] = params['filename']
                    except KeyError:
                        raise BadRequest('No title given, and couldn\'t get one from the URL\'s HTTP response')

                else:
                    entry['title'] = item.get('title')
                if item.get('force'):
                    entry['immortal'] = True
                if item.get('accept'):
                    entry.accept(reason='accepted by API inject')
                if item.get('fields'):
                    for key, value in item.get('fields').items():
                        entry[key] = value
                entries.append(entry)
            options['inject'] = entries

        executed_tasks = self.manager.execute(options=options, output=output, loglevel=loglevel)

        tasks_queued = []

        for task_id, task_name, task_event in executed_tasks:
            tasks_queued.append({'id': task_id, 'name': task_name, 'event': task_event})
            _streams[task_id] = {
                'queue': queue,
                'last_update': datetime.now(),
                'args': data
            }

        if not stream:
            return jsonify({'tasks': [{'id': task['id'], 'name': task['name']} for task in tasks_queued]})

        def stream_response():
            # First return the tasks to execute
            yield '{"stream": ['
            yield json.dumps({'tasks': [{'id': task['id'], 'name': task['name']} for task in tasks_queued]}) + ',\n'

            while True:
                try:
                    yield queue.get(timeout=1) + ',\n'
                    continue
                except Empty:
                    pass

                if queue.empty() and all([task['event'].is_set() for task in tasks_queued]):
                    for task in tasks_queued:
                        del _streams[task['id']]
                    break
            yield '{}]}'

        return Response(stream_response(), mimetype='text/event-stream')


@event('manager.daemon.started')
def setup_params(mgr):
    parser = get_parser('execute')

    for action in parser._optionals._actions:
        ignore = ['v', 's', 'try-regexp', 'dump-config']
        name = action.option_strings[0].strip('--')
        if name in ignore:
            continue
        if isinstance(action, argparse._StoreConstAction) and action.help != '==SUPPRESS==':
            name = name.replace('-', '_')
            ObjectsContainer.task_execution_input['properties'][name] = {'type': 'boolean'}
            TaskExecutionAPI.__apidoc__['description'] += "'{0}': {1}<br>".format(name, action.help)


class EntryDecoder(JSONEncoder):
    def default(self, o):
        if isinstance(o, LazyLookup):
            return '<LazyField>'

        try:
            return JSONEncoder.default(self, o)
        except TypeError:
            return str(o)


_phase_percents = {
    'input': 5,
    'metainfo': 10,
    'filter': 30,
    'download': 40,
    'modify': 65,
    'output': 75,
    'exit': 100,
}


def update_stream(task, status='pending'):
    if task.current_phase in _phase_percents:
        task.stream['percent'] = _phase_percents[task.current_phase]

    progress = {
        'status': status,
        'phase': task.current_phase,
        'plugin': task.current_plugin,
        'percent': task.stream.get('percent', 0)
    }

    task.stream['queue'].put(json.dumps({'progress': progress}))


@event('task.execute.started')
def start_task(task):
    task.stream = _streams.get(task.id)

    if task.stream and task.stream['args'].get('progress'):
        update_stream(task, status='running')


@event('task.execute.completed')
def finish_task(task):
    if task.stream:
        if task.stream['args'].get('progress'):
            update_stream(task, status='complete')

        if task.stream['args'].get('entry_dump'):
            entries = [entry.store for entry in task.entries]
            task.stream['queue'].put(EntryDecoder().encode({'entry_dump': entries}))

        if task.stream['args'].get('summary'):
            task.stream['queue'].put(json.dumps({
                'summary': {
                    'accepted': len(task.accepted),
                    'rejected': len(task.rejected),
                    'failed': len(task.failed),
                    'undecided': len(task.undecided),
                    'aborted': task.aborted,
                    'abort_reason': task.abort_reason,
                }
            }))


@event('task.execute.before_plugin')
def track_progress(task, plugin_name):
    if task.stream and task.stream['args'].get('progress'):
        update_stream(task, status='running')