import asyncio
import inspect
import json
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional

import websockets
from rx.subject import BehaviorSubject

import deepkit.globals
from deepkit.home import get_home_config
from deepkit.model import ContextOptions, FolderLink


def is_in_directory(filepath, directory):
    return os.path.realpath(filepath).startswith(os.path.realpath(directory))


class ApiError(Exception):
    pass


class Client(threading.Thread):
    connection: websockets.WebSocketClientProtocol

    def __init__(self, options: ContextOptions):
        self.connected = BehaviorSubject(False)
        self.options: ContextOptions = options

        self.loop = asyncio.new_event_loop()
        self.host = os.environ.get('DEEPKIT_HOST', '127.0.0.1')
        self.port = int(os.environ.get('DEEPKIT_PORT', '8960'))
        self.token = os.environ.get('DEEPKIT_JOB_ACCESSTOKEN', None)
        self.job_id = os.environ.get('DEEPKIT_JOB_ID', None)
        self.message_id = 0
        self.account = 'localhost'
        self.callbacks: Dict[int, asyncio.Future] = {}
        self.subscriber: Dict[int, any] = {}
        self.stopping = False
        self.queue = []
        self.controllers = {}
        self.patches = {}
        self.offline = False
        self.connections = 0
        self.lock = threading.Lock()
        threading.Thread.__init__(self)
        self.daemon = True
        self.loop = asyncio.new_event_loop()
        self.start()

    def run(self):
        self.connecting = self.loop.create_future()
        self.loop.create_task(self._connect())
        self.loop.run_forever()

    def shutdown(self):
        if self.offline: return
        promise = asyncio.run_coroutine_threadsafe(self.stop_and_sync(), self.loop)
        promise.result()
        if not self.connection.closed:
            raise Exception('Connection still active')
        self.loop.stop()

    async def stop_and_sync(self):
        self.stopping = True

        # done = 150, //when all tasks are done
        # aborted = 200, //when at least one task aborted
        # failed = 250, //when at least one task failed
        # crashed = 300, //when at least one task crashed
        self.patches['status'] = 150
        self.patches['ended'] = datetime.utcnow().isoformat()
        self.patches['tasks.main.ended'] = datetime.utcnow().isoformat()

        # done = 500,
        # aborted = 550,
        # failed = 600,
        # crashed = 650,
        self.patches['tasks.main.status'] = 500
        self.patches['tasks.main.instances.0.ended'] = datetime.utcnow().isoformat()

        # done = 500,
        # aborted = 550,
        # failed = 600,
        # crashed = 650,
        self.patches['tasks.main.instances.0.status'] = 500

        if hasattr(sys, 'last_value'):
            if isinstance(sys.last_value, KeyboardInterrupt):
                self.patches['status'] = 200
                self.patches['tasks.main.status'] = 550
                self.patches['tasks.main.instances.0.status'] = 550
            else:
                self.patches['status'] = 300
                self.patches['tasks.main.status'] = 650
                self.patches['tasks.main.instances.0.status'] = 650

        while len(self.patches) > 0 or len(self.queue) > 0:
            await asyncio.sleep(0.15)

        await self.connection.close()

    async def register_controller(self, name: str, controller):
        self.controllers[name] = controller

        async def subscriber(message, done):
            if message['type'] == 'error':
                done()
                del self.controllers[name]
                raise Exception('Register controller error: ' + message['error'])

            if message['type'] == 'ack':
                pass

            if message['type'] == 'peerController/message':
                data = message['data']

                if not hasattr(controller, data['action']):
                    error = f"Requested action {message['action']} not available in {name}"
                    print(error, file=sys.stderr)
                    await self._message({
                        'name': 'peerController/message',
                        'controllerName': name,
                        'clientId': message['clientId'],
                        'data': {'type': 'error', 'id': data['id'], 'stack': None, 'entityName': '@error:default',
                                 'error': error}
                    }, no_response=True)

                if data['name'] == 'actionTypes':
                    parameters = []

                    i = 0
                    for arg in inspect.getfullargspec(getattr(controller, data['action'])).args:
                        parameters.append({
                            'type': 'any',
                            'name': '#' + str(i)
                        })
                        i += 1

                    await self._message({
                        'name': 'peerController/message',
                        'controllerName': name,
                        'clientId': message['clientId'],
                        'data': {
                            'type': 'actionTypes/result',
                            'id': data['id'],
                            'parameters': parameters,
                            'returnType': {'type': 'any', 'name': 'result'}
                        }
                    }, no_response=True)

                if data['name'] == 'action':
                    try:
                        res = getattr(controller, data['action'])(*data['args'])

                        await self._message({
                            'name': 'peerController/message',
                            'controllerName': name,
                            'clientId': message['clientId'],
                            'data': {
                                'type': 'next/json',
                                'id': data['id'],
                                'encoding': {'name': 'r', 'type': 'any'},
                                'next': res,
                            }
                        }, no_response=True)
                    except Exception as e:
                        await self._message({
                            'name': 'peerController/message',
                            'controllerName': name,
                            'clientId': message['clientId'],
                            'data': {'type': 'error', 'id': data['id'], 'stack': None, 'entityName': '@error:default',
                                     'error': str(e)}
                        }, no_response=True)

        await self._subscribe({
            'name': 'peerController/register',
            'controllerName': name,
        }, subscriber)

        class Controller:
            def __init__(self, client):
                self.client = client

            def stop(self):
                self.client._message({
                    'name': 'peerController/unregister',
                    'controllerName': name,
                })

        return Controller(self)

    async def _action(self, controller: str, action: str, args: List, lock=True, allow_in_shutdown=False):
        if lock: await self.connecting
        if self.offline: return
        if self.stopping and not allow_in_shutdown: raise Exception('In shutdown: actions disallowed')

        if not controller: raise Exception('No controller given')
        if not action: raise Exception('No action given')

        res = await self._message({
            'name': 'action',
            'controller': controller,
            'action': action,
            'args': args,
            'timeout': 60
        }, lock=lock)

        if res['type'] == 'next/json':
            return res['next'] if 'next' in res else None

        if res['type'] == 'error':
            print(res, file=sys.stderr)
            raise ApiError('API Error: ' + str(res['error']))

        raise ApiError(f"Invalid action type '{res['type']}'. Not implemented")

    def job_action(self, action: str, args: List):
        return asyncio.run_coroutine_threadsafe(self._action('job', action, args), self.loop)

    async def _subscribe(self, message, subscriber):
        await self.connecting

        self.message_id += 1
        message['id'] = self.message_id

        message_id = self.message_id

        def on_done():
            del self.subscriber[message_id]

        async def on_incoming_message(incoming_message):
            await subscriber(incoming_message, on_done)

        self.subscriber[self.message_id] = on_incoming_message
        self.queue.append(message)

    async def _message(self, message, lock=True, no_response=False):
        if lock: await self.connecting

        self.message_id += 1
        message['id'] = self.message_id
        if not no_response:
            self.callbacks[self.message_id] = self.loop.create_future()

        self.queue.append(message)

        if no_response:
            return

        return await self.callbacks[self.message_id]

    def patch(self, path: str, value: any):
        if self.offline: return
        if self.stopping: return

        self.patches[path] = value

    async def send_messages(self, connection):
        while not connection.closed:
            try:
                q = self.queue[:]
                for m in q:
                    await connection.send(json.dumps(m))
                    self.queue.remove(m)
            except Exception as e:
                print("Failed sending, exit send_messages")
                raise e

            if len(self.patches) > 0:
                # we have to send first all messages/actions out
                # before sending patches, as most of the time
                # patches are based on previously created entities,
                # so we need to make sure those entities are created
                # first before sending any patches.
                try:
                    send = self.patches.copy()
                    await connection.send(json.dumps({
                        'name': 'action',
                        'controller': 'job',
                        'action': 'patchJob',
                        'args': [
                            send
                        ],
                        'timeout': 60
                    }))

                    for i in send.keys():
                        if self.patches[i] == send[i]:
                            del self.patches[i]
                except websockets.exceptions.ConnectionClosed:
                    return
                except ApiError:
                    print("Patching failed. Syncing job data disabled.", file=sys.stderr)
                    return

            await asyncio.sleep(0.2)

    async def handle_messages(self, connection):
        while not connection.closed:
            try:
                res = json.loads(await connection.recv())
            except websockets.exceptions.ConnectionClosedError:
                # we need reconnect
                break
            except websockets.exceptions.ConnectionClosedOK:
                # we closed on purpose, so no reconnect necessary
                return

            if res and 'id' in res:
                if res['id'] in self.subscriber:
                    await self.subscriber[res['id']](res)

                if res['id'] in self.callbacks:
                    self.callbacks[res['id']].set_result(res)
                    del self.callbacks[res['id']]

        if not self.stopping:
            print("Deepkit: lost connection. reconnect ...")
            self.connecting = self.loop.create_future()
            self.connected.on_next(False)
            self.loop.create_task(self._connect())

    async def _connect_job(self, host: str, port: int, id: str, token: str):
        try:
            self.connection = await websockets.connect(f"ws://{host}:{port}")
        except Exception:
            # try again later
            await asyncio.sleep(1)
            self.loop.create_task(self._connect())
            return

        self.loop.create_task(self.handle_messages(self.connection))
        self.loop.create_task(self.send_messages(self.connection))

        res = await self._message({
            'name': 'authenticate',
            'token': {
                'id': 'job',
                'token': token,
                'job': id
            }
        }, lock=False)

        if not res['result'] or res['result'] is not True:
            raise Exception('Job token invalid')

        self.connecting.set_result(True)
        if self.connections > 0:
            print("Deepkit: Reconnected.")

        self.connected.on_next(True)
        self.connections += 1

    async def _connect(self):
        # we want to restart with a empty queue, so authentication happens always first
        queue_copy = self.queue[:]
        self.queue = []

        if self.token:
            await self._connect_job(self.host, self.port, self.job_id, self.token)
        else:
            config = get_home_config()
            link: Optional[FolderLink] = None
            if self.options.account:
                account_config = config.get_account_for_name(self.options.account)
            else:
                link = config.get_folder_link_of_directory(sys.path[0])
                account_config = config.get_account_for_id(link.accountId)

            self.host = account_config.host
            self.port = account_config.port
            ws = 'wss' if account_config.ssl else 'ws'

            try:
                self.connection = await websockets.connect(f"{ws}://{self.host}:{self.port}")
            except Exception as e:
                self.offline = True
                print(f"Deepkit: App not started or server not reachable. Monitoring disabled. {e}")
                self.connecting.set_result(False)
                return

            self.loop.create_task(self.handle_messages(self.connection))
            self.loop.create_task(self.send_messages(self.connection))
            res = await self._message({
                'name': 'authenticate',
                'token': {
                    'id': 'user',
                    'token': account_config.token
                }
            }, lock=False)
            if not res['result']:
                raise Exception('Login invalid')

            if link:
                projectId = link.projectId
            else:
                if not self.options.project:
                    raise Exception('No project defined. Please use ContextOptions(project="project-name") '
                                    'to specify which project to use.')

                project = await self._action('app', 'getProjectForPublicName', [self.options.project], lock=False)
                if not project:
                    raise Exception(f'No project found for name {self.options.project}. '
                                    f'Do you use the correct account? (used {account_config.name})')

                projectId = project['id']

            job = await self._action('app', 'createJob', [projectId],
                                     lock=False)

            deepkit.globals.loaded_job = job
            self.token = await self._action('app', 'getJobAccessToken', [job['id']], lock=False)
            self.job_id = job['id']

            # todo, implement re-authentication, so we don't have to drop the active connection
            await self.connection.close()
            await self._connect_job(self.host, self.port, self.job_id, self.token)

        self.queue = queue_copy + self.queue
