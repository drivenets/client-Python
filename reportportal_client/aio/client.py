"""This module contains asynchronous implementation of Report Portal Client."""

#  Copyright (c) 2023 EPAM Systems
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License

import asyncio
import logging
import ssl
import sys
import threading
import time
import warnings
from os import getenv
from queue import LifoQueue
from typing import Union, Tuple, List, Dict, Any, Optional, TextIO, Coroutine, TypeVar

import aiohttp
import certifi

from reportportal_client import RP
# noinspection PyProtectedMember
from reportportal_client._local import set_current
from reportportal_client.aio import (Task, BatchedTaskFactory, ThreadedTaskFactory, DEFAULT_TASK_TIMEOUT,
                                     DEFAULT_SHUTDOWN_TIMEOUT, DEFAULT_TASK_TRIGGER_NUM,
                                     DEFAULT_TASK_TRIGGER_INTERVAL)
from reportportal_client.core.rp_issues import Issue
from reportportal_client.core.rp_requests import (LaunchStartRequest, AsyncHttpRequest, AsyncItemStartRequest,
                                                  AsyncItemFinishRequest, LaunchFinishRequest, RPFile,
                                                  AsyncRPRequestLog, AsyncRPLogBatch)
from reportportal_client.helpers import (root_uri_join, verify_value_length, await_if_necessary,
                                         agent_name_version)
from reportportal_client.logs import MAX_LOG_BATCH_PAYLOAD_SIZE
from reportportal_client.logs.batcher import LogBatcher
from reportportal_client.services.statistics import async_send_event
from reportportal_client.static.abstract import (
    AbstractBaseClass,
    abstractmethod
)
from reportportal_client.static.defines import NOT_FOUND
from reportportal_client.steps import StepReporter

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_T = TypeVar('_T')


class _LifoQueue(LifoQueue):
    def last(self):
        with self.mutex:
            if self._qsize():
                return self.queue[-1]


class Client:
    api_v1: str
    api_v2: str
    base_url_v1: str
    base_url_v2: str
    endpoint: str
    is_skipped_an_issue: bool
    log_batch_size: int
    log_batch_payload_size: int
    project: str
    api_key: str
    verify_ssl: Union[bool, str]
    retries: int
    max_pool_size: int
    http_timeout: Union[float, Tuple[float, float]]
    mode: str
    launch_uuid_print: Optional[bool]
    print_output: Optional[TextIO]
    _skip_analytics: str
    __session: Optional[aiohttp.ClientSession]
    __stat_task: Optional[asyncio.Task[aiohttp.ClientResponse]]

    def __init__(
            self,
            endpoint: str,
            project: str,
            *,
            api_key: str = None,
            log_batch_size: int = 20,
            is_skipped_an_issue: bool = True,
            verify_ssl: Union[bool, str] = True,
            retries: int = None,
            max_pool_size: int = 50,
            http_timeout: Union[float, Tuple[float, float]] = (10, 10),
            log_batch_payload_size: int = MAX_LOG_BATCH_PAYLOAD_SIZE,
            mode: str = 'DEFAULT',
            launch_uuid_print: bool = False,
            print_output: Optional[TextIO] = None,
            **kwargs: Any
    ) -> None:
        self.api_v1, self.api_v2 = 'v1', 'v2'
        self.endpoint = endpoint
        self.project = project
        self.base_url_v1 = root_uri_join(f'api/{self.api_v1}', self.project)
        self.base_url_v2 = root_uri_join(f'api/{self.api_v2}', self.project)
        self.is_skipped_an_issue = is_skipped_an_issue
        self.log_batch_size = log_batch_size
        self.log_batch_payload_size = log_batch_payload_size
        self.verify_ssl = verify_ssl
        self.retries = retries
        self.max_pool_size = max_pool_size
        self.http_timeout = http_timeout
        self.mode = mode
        self._skip_analytics = getenv('AGENT_NO_ANALYTICS')
        self.launch_uuid_print = launch_uuid_print
        self.print_output = print_output or sys.stdout
        self.__session = None
        self.__stat_task = None

        self.api_key = api_key
        if not self.api_key:
            if 'token' in kwargs:
                warnings.warn(
                    message='Argument `token` is deprecated since 5.3.5 and '
                            'will be subject for removing in the next major '
                            'version. Use `api_key` argument instead.',
                    category=DeprecationWarning,
                    stacklevel=2
                )
                self.api_key = kwargs['token']

            if not self.api_key:
                warnings.warn(
                    message='Argument `api_key` is `None` or empty string, '
                            'that is not supposed to happen because Report '
                            'Portal is usually requires an authorization key. '
                            'Please check your code.',
                    category=RuntimeWarning,
                    stacklevel=2
                )

    @property
    def session(self) -> aiohttp.ClientSession:
        # TODO: add retry handler
        if self.__session:
            return self.__session

        ssl_config = self.verify_ssl
        if ssl_config:
            if type(ssl_config) == str:
                sl_config = ssl.create_default_context()
                sl_config.load_cert_chain(ssl_config)
            else:
                ssl_config = ssl.create_default_context(cafile=certifi.where())

        connector = aiohttp.TCPConnector(ssl=ssl_config, limit=self.max_pool_size)

        timeout = None
        if self.http_timeout:
            if type(self.http_timeout) == tuple:
                connect_timeout, read_timeout = self.http_timeout
            else:
                connect_timeout, read_timeout = self.http_timeout, self.http_timeout
            timeout = aiohttp.ClientTimeout(connect=connect_timeout, sock_read=read_timeout)

        headers = {}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        self.__session = aiohttp.ClientSession(self.endpoint, connector=connector, headers=headers,
                                               timeout=timeout)
        return self.__session

    async def __get_item_url(self, item_id_future: Union[str, Task[str]]) -> Optional[str]:
        item_id = await await_if_necessary(item_id_future)
        if item_id is NOT_FOUND:
            logger.warning('Attempt to make request for non-existent id.')
            return
        return root_uri_join(self.base_url_v2, 'item', item_id)

    async def __get_launch_url(self, launch_uuid_future: Union[str, Task[str]]) -> Optional[str]:
        launch_uuid = await await_if_necessary(launch_uuid_future)
        if launch_uuid is NOT_FOUND:
            logger.warning('Attempt to make request for non-existent launch.')
            return
        return root_uri_join(self.base_url_v2, 'launch', launch_uuid, 'finish')

    async def start_launch(self,
                           name: str,
                           start_time: str,
                           *,
                           description: Optional[str] = None,
                           attributes: Optional[Union[List, Dict]] = None,
                           rerun: bool = False,
                           rerun_of: Optional[str] = None,
                           **kwargs) -> Optional[str]:
        """Start a new launch with the given parameters.

        :param name:        Launch name
        :param start_time:  Launch start time
        :param description: Launch description
        :param attributes:  Launch attributes
        :param rerun:       Start launch in rerun mode
        :param rerun_of:    For rerun mode specifies which launch will be
                            re-run. Should be used with the 'rerun' option.
        """
        url = root_uri_join(self.base_url_v2, 'launch')
        request_payload = LaunchStartRequest(
            name=name,
            start_time=start_time,
            attributes=attributes,
            description=description,
            mode=self.mode,
            rerun=rerun,
            rerun_of=rerun_of or kwargs.get('rerunOf')
        ).payload

        response = await AsyncHttpRequest(self.session.post, url=url, json=request_payload).make()
        if not response:
            return

        if not self._skip_analytics:
            stat_coro = async_send_event('start_launch', *agent_name_version(attributes))
            self.__stat_task = asyncio.create_task(stat_coro, name='Statistics update')

        launch_uuid = await response.id
        logger.debug(f'start_launch - ID: %s', launch_uuid)
        if self.launch_uuid_print and self.print_output:
            print(f'Report Portal Launch UUID: {launch_uuid}', file=self.print_output)
        return launch_uuid

    async def start_test_item(self,
                              launch_uuid: Union[str, Task[str]],
                              name: str,
                              start_time: str,
                              item_type: str,
                              *,
                              description: Optional[str] = None,
                              attributes: Optional[List[Dict]] = None,
                              parameters: Optional[Dict] = None,
                              parent_item_id: Optional[Union[str, Task[str]]] = None,
                              has_stats: bool = True,
                              code_ref: Optional[str] = None,
                              retry: bool = False,
                              test_case_id: Optional[str] = None,
                              **_: Any) -> Optional[str]:
        if parent_item_id:
            url = self.__get_item_url(parent_item_id)
        else:
            url = root_uri_join(self.base_url_v2, 'item')
        request_payload = AsyncItemStartRequest(
            name,
            start_time,
            item_type,
            launch_uuid,
            attributes=verify_value_length(attributes),
            code_ref=code_ref,
            description=description,
            has_stats=has_stats,
            parameters=parameters,
            retry=retry,
            test_case_id=test_case_id
        ).payload

        response = await AsyncHttpRequest(self.session.post, url=url, json=request_payload).make()
        if not response:
            return
        item_id = await response.id
        if item_id is NOT_FOUND:
            logger.warning('start_test_item - invalid response: %s', str(await response.json))
        else:
            logger.debug('start_test_item - ID: %s', item_id)
        return item_id

    async def finish_test_item(self,
                               launch_uuid: Union[str, Task[str]],
                               item_id: Union[str, Task[str]],
                               end_time: str,
                               *,
                               status: str = None,
                               issue: Optional[Issue] = None,
                               attributes: Optional[Union[List, Dict]] = None,
                               description: str = None,
                               retry: bool = False,
                               **kwargs: Any) -> Optional[str]:
        url = self.__get_item_url(item_id)
        request_payload = AsyncItemFinishRequest(
            end_time,
            launch_uuid,
            status,
            attributes=attributes,
            description=description,
            is_skipped_an_issue=self.is_skipped_an_issue,
            issue=issue,
            retry=retry
        ).payload
        response = await AsyncHttpRequest(self.session.put, url=url, json=request_payload).make()
        if not response:
            return
        message = await response.message
        logger.debug('finish_test_item - ID: %s', await await_if_necessary(item_id))
        logger.debug('response message: %s', message)
        return message

    async def finish_launch(self,
                            launch_uuid: Union[str, Task[str]],
                            end_time: str,
                            *,
                            status: str = None,
                            attributes: Optional[Union[List, Dict]] = None,
                            **kwargs: Any) -> Optional[str]:
        url = self.__get_launch_url(launch_uuid)
        request_payload = LaunchFinishRequest(
            end_time,
            status=status,
            attributes=attributes,
            description=kwargs.get('description')
        ).payload
        response = await AsyncHttpRequest(self.session.put, url=url, json=request_payload,
                                          name='Finish Launch').make()
        if not response:
            return
        message = await response.message
        logger.debug('finish_launch - ID: %s', await await_if_necessary(launch_uuid))
        logger.debug('response message: %s', message)
        return message

    async def update_test_item(self,
                               item_uuid: Union[str, Task[str]],
                               *,
                               attributes: Optional[Union[List, Dict]] = None,
                               description: Optional[str] = None) -> Optional[str]:
        data = {
            'description': description,
            'attributes': verify_value_length(attributes),
        }
        item_id = await self.get_item_id_by_uuid(item_uuid)
        url = root_uri_join(self.base_url_v1, 'item', item_id, 'update')
        response = await AsyncHttpRequest(self.session.put, url=url, json=data).make()
        if not response:
            return
        logger.debug('update_test_item - Item: %s', item_id)
        return await response.message

    async def __get_item_uuid_url(self, item_uuid_future: Union[str, Task[str]]) -> Optional[str]:
        item_uuid = await await_if_necessary(item_uuid_future)
        if item_uuid is NOT_FOUND:
            logger.warning('Attempt to make request for non-existent UUID.')
            return
        return root_uri_join(self.base_url_v1, 'item', 'uuid', item_uuid)

    async def get_item_id_by_uuid(self, item_uuid_future: Union[str, Task[str]]) -> Optional[str]:
        """Get test Item ID by the given Item UUID.

        :param item_uuid_future: Str or Task UUID returned on the Item start
        :return:                 Test item ID
        """
        url = self.__get_item_uuid_url(item_uuid_future)
        response = await AsyncHttpRequest(self.session.get, url=url).make()
        return response.id if response else None

    async def __get_launch_uuid_url(self, launch_uuid_future: Union[str, Task[str]]) -> Optional[str]:
        launch_uuid = await await_if_necessary(launch_uuid_future)
        if launch_uuid is NOT_FOUND:
            logger.warning('Attempt to make request for non-existent Launch UUID.')
            return
        logger.debug('get_launch_info - ID: %s', launch_uuid)
        return root_uri_join(self.base_url_v1, 'launch', 'uuid', launch_uuid)

    async def get_launch_info(self, launch_uuid_future: Union[str, Task[str]]) -> Optional[Dict]:
        """Get the launch information by Launch UUID.

        :param launch_uuid_future: Str or Task UUID returned on the Launch start
        :return dict:              Launch information in dictionary
        """
        url = self.__get_launch_uuid_url(launch_uuid_future)
        response = await AsyncHttpRequest(self.session.get, url=url).make()
        if not response:
            return
        if response.is_success:
            launch_info = await response.json
            logger.debug('get_launch_info - Launch info: %s', launch_info)
        else:
            logger.warning('get_launch_info - Launch info: Failed to fetch launch ID from the API.')
            launch_info = {}
        return launch_info

    async def get_launch_ui_id(self, launch_uuid_future: Union[str, Task[str]]) -> Optional[int]:
        launch_info = await self.get_launch_info(launch_uuid_future)
        return launch_info.get('id') if launch_info else None

    async def get_launch_ui_url(self, launch_uuid_future: Union[str, Task[str]]) -> Optional[str]:
        launch_uuid = await await_if_necessary(launch_uuid_future)
        launch_info = await self.get_launch_info(launch_uuid)
        ui_id = launch_info.get('id') if launch_info else None
        if not ui_id:
            return
        mode = launch_info.get('mode') if launch_info else None
        if not mode:
            mode = self.mode

        launch_type = 'launches' if mode.upper() == 'DEFAULT' else 'userdebug'

        path = 'ui/#{project_name}/{launch_type}/all/{launch_id}'.format(
            project_name=self.project.lower(), launch_type=launch_type,
            launch_id=ui_id)
        url = root_uri_join(self.endpoint, path)
        logger.debug('get_launch_ui_url - ID: %s', launch_uuid)
        return url

    async def get_project_settings(self) -> Optional[Dict]:
        url = root_uri_join(self.base_url_v1, 'settings')
        response = await AsyncHttpRequest(self.session.get, url=url).make()
        return await response.json if response else None

    async def log_batch(self, log_batch: Optional[List[AsyncRPRequestLog]]) -> Tuple[str, ...]:
        url = root_uri_join(self.base_url_v2, 'log')
        if log_batch:
            response = await AsyncHttpRequest(self.session.post, url=url,
                                              data=AsyncRPLogBatch(log_batch)).make()
            return await response.messages

    def clone(self) -> 'Client':
        """Clone the client object, set current Item ID as cloned item ID.

        :returns: Cloned client object
        :rtype: AsyncRPClient
        """
        cloned = Client(
            endpoint=self.endpoint,
            project=self.project,
            api_key=self.api_key,
            log_batch_size=self.log_batch_size,
            is_skipped_an_issue=self.is_skipped_an_issue,
            verify_ssl=self.verify_ssl,
            retries=self.retries,
            max_pool_size=self.max_pool_size,
            http_timeout=self.http_timeout,
            log_batch_payload_size=self.log_batch_payload_size,
            mode=self.mode
        )
        return cloned


class AsyncRPClient(RP):
    _item_stack: _LifoQueue
    _log_batcher: LogBatcher
    __client: Client
    __launch_uuid: Optional[str]
    use_own_launch: bool
    step_reporter: StepReporter

    def __init__(self, endpoint: str, project: str, *, launch_uuid: Optional[str] = None,
                 client: Optional[Client] = None, **kwargs: Any) -> None:
        set_current(self)
        self.step_reporter = StepReporter(self)
        self._item_stack = _LifoQueue()
        self._log_batcher = LogBatcher()
        if client:
            self.__client = client
        else:
            self.__client = Client(endpoint, project, **kwargs)
        if launch_uuid:
            self.launch_uuid = launch_uuid
            self.use_own_launch = False
        else:
            self.use_own_launch = True

    async def start_launch(self,
                           name: str,
                           start_time: str,
                           description: Optional[str] = None,
                           attributes: Optional[Union[List, Dict]] = None,
                           rerun: bool = False,
                           rerun_of: Optional[str] = None,
                           **kwargs) -> Optional[str]:
        if not self.use_own_launch:
            return self.launch_uuid
        launch_uuid = await self.__client.start_launch(name, start_time, description=description,
                                                       attributes=attributes, rerun=rerun, rerun_of=rerun_of,
                                                       **kwargs)
        self.launch_uuid = launch_uuid
        return launch_uuid

    async def start_test_item(self,
                              name: str,
                              start_time: str,
                              item_type: str,
                              *,
                              description: Optional[str] = None,
                              attributes: Optional[List[Dict]] = None,
                              parameters: Optional[Dict] = None,
                              parent_item_id: Optional[str] = None,
                              has_stats: bool = True,
                              code_ref: Optional[str] = None,
                              retry: bool = False,
                              test_case_id: Optional[str] = None,
                              **kwargs: Any) -> Optional[str]:
        item_id = await self.__client.start_test_item(self.launch_uuid, name, start_time, item_type,
                                                      description=description, attributes=attributes,
                                                      parameters=parameters, parent_item_id=parent_item_id,
                                                      has_stats=has_stats, code_ref=code_ref, retry=retry,
                                                      test_case_id=test_case_id, **kwargs)
        if item_id and item_id is not NOT_FOUND:
            logger.debug('start_test_item - ID: %s', item_id)
            self._add_current_item(item_id)
        return item_id

    async def finish_test_item(self,
                               item_id: str,
                               end_time: str,
                               *,
                               status: str = None,
                               issue: Optional[Issue] = None,
                               attributes: Optional[Union[List, Dict]] = None,
                               description: str = None,
                               retry: bool = False,
                               **kwargs: Any) -> Optional[str]:
        result = await self.__client.finish_test_item(self.launch_uuid, item_id, end_time, status=status,
                                                      issue=issue, attributes=attributes,
                                                      description=description,
                                                      retry=retry, **kwargs)
        self._remove_current_item()
        return result

    async def finish_launch(self,
                            end_time: str,
                            status: str = None,
                            attributes: Optional[Union[List, Dict]] = None,
                            **kwargs: Any) -> Optional[str]:
        await self.__client.log_batch(self._log_batcher.flush())
        if not self.use_own_launch:
            return ""
        return await self.__client.finish_launch(self.launch_uuid, end_time, status=status,
                                                 attributes=attributes,
                                                 **kwargs)

    async def update_test_item(self, item_uuid: str, attributes: Optional[Union[List, Dict]] = None,
                               description: Optional[str] = None) -> Optional[str]:
        return await self.__client.update_test_item(item_uuid, attributes=attributes, description=description)

    def _add_current_item(self, item: str) -> None:
        """Add the last item from the self._items queue."""
        self._item_stack.put(item)

    def _remove_current_item(self) -> Optional[str]:
        """Remove the last item from the self._items queue."""
        return self._item_stack.get()

    def current_item(self) -> Optional[str]:
        """Retrieve the last item reported by the client."""
        return self._item_stack.last()

    async def get_launch_info(self) -> Optional[dict]:
        if not self.launch_uuid:
            return {}
        return await self.__client.get_launch_info(self.launch_uuid)

    async def get_item_id_by_uuid(self, item_uuid: str) -> Optional[str]:
        return await self.__client.get_item_id_by_uuid(item_uuid)

    async def get_launch_ui_id(self) -> Optional[int]:
        if not self.launch_uuid:
            return
        return await self.__client.get_launch_ui_id(self.launch_uuid)

    async def get_launch_ui_url(self) -> Optional[str]:
        if not self.launch_uuid:
            return
        return await self.__client.get_launch_ui_url(self.launch_uuid)

    async def get_project_settings(self) -> Optional[Dict]:
        return await self.__client.get_project_settings()

    async def log(self, datetime: str, message: str, level: Optional[Union[int, str]] = None,
                  attachment: Optional[Dict] = None,
                  parent_item: Optional[str] = None) -> Optional[Tuple[str, ...]]:
        """Log message. Can be added to test item in any state.

        :param datetime:    Log time
        :param message:     Log message
        :param level:       Log level
        :param attachment:  Attachments(images,files,etc.)
        :param parent_item: Parent item UUID
        """
        if parent_item is NOT_FOUND:
            logger.warning("Attempt to log to non-existent item")
            return
        rp_file = RPFile(**attachment) if attachment else None
        rp_log = AsyncRPRequestLog(self.launch_uuid, datetime, rp_file, parent_item, level, message)
        return await self.__client.log_batch(await self._log_batcher.append_async(rp_log))

    @property
    def launch_uuid(self) -> Optional[str]:
        return self.__launch_uuid

    @launch_uuid.setter
    def launch_uuid(self, value: Optional[str]) -> None:
        self.__launch_uuid = value

    def clone(self) -> 'AsyncRPClient':
        """Clone the client object, set current Item ID as cloned item ID.

        :returns: Cloned client object
        :rtype: AsyncRPClient
        """
        cloned_client = self.__client.clone()
        # noinspection PyTypeChecker
        cloned = AsyncRPClient(
            endpoint=None,
            project=None,
            client=cloned_client,
            launch_uuid=self.launch_uuid
        )
        current_item = self.current_item()
        if current_item:
            cloned._add_current_item(current_item)
        return cloned


class _SyncRPClient(RP, metaclass=AbstractBaseClass):
    __metaclass__ = AbstractBaseClass

    _item_stack: _LifoQueue
    _log_batcher: LogBatcher
    __launch_uuid: Optional[Task[str]]
    use_own_launch: bool
    step_reporter: StepReporter

    @property
    def launch_uuid(self) -> Optional[Task[str]]:
        return self.__launch_uuid

    @launch_uuid.setter
    def launch_uuid(self, value: Optional[Task[str]]) -> None:
        self.__launch_uuid = value

    def __init__(self, endpoint: str, project: str, *, launch_uuid: Optional[Task[str]] = None,
                 client: Optional[Client] = None, **kwargs: Any) -> None:
        set_current(self)
        self.step_reporter = StepReporter(self)
        self._item_stack = _LifoQueue()
        self._log_batcher = LogBatcher()
        if client:
            self.__client = client
        else:
            self.__client = Client(endpoint, project, **kwargs)
        if launch_uuid:
            self.launch_uuid = launch_uuid
            self.use_own_launch = False
        else:
            self.use_own_launch = True

    @abstractmethod
    def create_task(self, coro: Coroutine[Any, Any, _T]) -> Task[_T]:
        raise NotImplementedError('"create_task" method is not implemented!')

    @abstractmethod
    def finish_tasks(self) -> None:
        raise NotImplementedError('"create_task" method is not implemented!')

    def _add_current_item(self, item: Task[_T]) -> None:
        """Add the last item from the self._items queue."""
        self._item_stack.put(item)

    def _remove_current_item(self) -> Task[_T]:
        """Remove the last item from the self._items queue."""
        return self._item_stack.get()

    def current_item(self) -> Task[_T]:
        """Retrieve the last item reported by the client."""
        return self._item_stack.last()

    async def __empty_str(self):
        return ""

    async def __empty_dict(self):
        return {}

    async def __int_value(self):
        return -1

    def start_launch(self,
                     name: str,
                     start_time: str,
                     description: Optional[str] = None,
                     attributes: Optional[Union[List, Dict]] = None,
                     rerun: bool = False,
                     rerun_of: Optional[str] = None,
                     **kwargs) -> Task[str]:
        if not self.use_own_launch:
            return self.launch_uuid
        launch_uuid_coro = self.__client.start_launch(name, start_time, description=description,
                                                      attributes=attributes, rerun=rerun, rerun_of=rerun_of,
                                                      **kwargs)
        self.launch_uuid = self.create_task(launch_uuid_coro)
        return self.launch_uuid

    def start_test_item(self,
                        name: str,
                        start_time: str,
                        item_type: str,
                        *,
                        description: Optional[str] = None,
                        attributes: Optional[List[Dict]] = None,
                        parameters: Optional[Dict] = None,
                        parent_item_id: Optional[Task[str]] = None,
                        has_stats: bool = True,
                        code_ref: Optional[str] = None,
                        retry: bool = False,
                        test_case_id: Optional[str] = None,
                        **kwargs: Any) -> Task[str]:

        item_id_coro = self.__client.start_test_item(self.launch_uuid, name, start_time, item_type,
                                                     description=description, attributes=attributes,
                                                     parameters=parameters, parent_item_id=parent_item_id,
                                                     has_stats=has_stats, code_ref=code_ref, retry=retry,
                                                     test_case_id=test_case_id, **kwargs)
        item_id_task = self.create_task(item_id_coro)
        self._add_current_item(item_id_task)
        return item_id_task

    def finish_test_item(self,
                         item_id: Task[str],
                         end_time: str,
                         *,
                         status: str = None,
                         issue: Optional[Issue] = None,
                         attributes: Optional[Union[List, Dict]] = None,
                         description: str = None,
                         retry: bool = False,
                         **kwargs: Any) -> Task[str]:
        result_coro = self.__client.finish_test_item(self.launch_uuid, item_id, end_time, status=status,
                                                     issue=issue, attributes=attributes,
                                                     description=description,
                                                     retry=retry, **kwargs)
        result_task = self.create_task(result_coro)
        self._remove_current_item()
        return result_task

    def finish_launch(self,
                      end_time: str,
                      status: str = None,
                      attributes: Optional[Union[List, Dict]] = None,
                      **kwargs: Any) -> Task[str]:
        self.create_task(self.__client.log_batch(self._log_batcher.flush()))
        if self.use_own_launch:
            result_coro = self.__client.finish_launch(self.launch_uuid, end_time, status=status,
                                                      attributes=attributes, **kwargs)
        else:
            result_coro = self.create_task(self.__empty_str())

        result_task = self.create_task(result_coro)
        self.finish_tasks()
        return result_task

    def update_test_item(self,
                         item_uuid: Task[str],
                         attributes: Optional[Union[List, Dict]] = None,
                         description: Optional[str] = None) -> Task:
        result_coro = self.__client.update_test_item(item_uuid, attributes=attributes,
                                                     description=description)
        result_task = self.create_task(result_coro)
        return result_task

    def get_launch_info(self) -> Task[dict]:
        if not self.launch_uuid:
            return self.create_task(self.__empty_dict())
        result_coro = self.__client.get_launch_info(self.launch_uuid)
        result_task = self.create_task(result_coro)
        return result_task

    def get_item_id_by_uuid(self, item_uuid_future: Task[str]) -> Task[str]:
        result_coro = self.__client.get_item_id_by_uuid(item_uuid_future)
        result_task = self.create_task(result_coro)
        return result_task

    def get_launch_ui_id(self) -> Task[int]:
        if not self.launch_uuid:
            return self.create_task(self.__int_value())
        result_coro = self.__client.get_launch_ui_id(self.launch_uuid)
        result_task = self.create_task(result_coro)
        return result_task

    def get_launch_ui_url(self) -> Task[str]:
        if not self.launch_uuid:
            return self.create_task(self.__empty_str())
        result_coro = self.__client.get_launch_ui_url(self.launch_uuid)
        result_task = self.create_task(result_coro)
        return result_task

    def get_project_settings(self) -> Task[dict]:
        result_coro = self.__client.get_project_settings()
        result_task = self.create_task(result_coro)
        return result_task

    async def _log(self, log_rq: AsyncRPRequestLog) -> Optional[Tuple[str, ...]]:
        return await self.__client.log_batch(await self._log_batcher.append_async(log_rq))

    def log(self, datetime: str, message: str, level: Optional[Union[int, str]] = None,
            attachment: Optional[Dict] = None, parent_item: Optional[Task[str]] = None) -> None:
        """Log message. Can be added to test item in any state.

        :param datetime:    Log time
        :param message:     Log message
        :param level:       Log level
        :param attachment:  Attachments(images,files,etc.)
        :param parent_item: Parent item UUID
        """
        if parent_item is NOT_FOUND:
            logger.warning("Attempt to log to non-existent item")
            return
        rp_file = RPFile(**attachment) if attachment else None
        rp_log = AsyncRPRequestLog(self.launch_uuid, datetime, rp_file, parent_item, level, message)
        self.create_task(self._log(rp_log))
        return None


class ThreadedRPClient(_SyncRPClient):
    __task_list: List[Task[_T]]
    __task_mutex: threading.Lock
    __loop: Optional[asyncio.AbstractEventLoop]
    __thread: Optional[threading.Thread]
    __self_loop: bool
    __task_timeout: float
    __shutdown_timeout: float

    def __init__(self, endpoint: str, project: str, *, launch_uuid: Optional[Task[str]] = None,
                 client: Optional[Client] = None,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 task_timeout: float = DEFAULT_TASK_TIMEOUT,
                 shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(endpoint, project, launch_uuid=launch_uuid, client=client, **kwargs)
        self.__task_timeout = task_timeout
        self.__shutdown_timeout = shutdown_timeout
        self.__task_list = []
        self.__task_mutex = threading.Lock()
        self.__thread = None
        if loop:
            self.__loop = loop
            self.__self_loop = False
        else:
            self.__loop = asyncio.new_event_loop()
            self.__loop.set_task_factory(ThreadedTaskFactory(self.__loop, self.__task_timeout))
            self.__self_loop = True

    def create_task(self, coro: Coroutine[Any, Any, _T]) -> Task[_T]:
        loop = self.__loop
        result = loop.create_task(coro)
        with self.__task_mutex:
            self.__task_list.append(result)
            if not self.__thread and self.__self_loop:
                self.__thread = threading.Thread(target=loop.run_forever, name='RP-Async-Client',
                                                 daemon=True)
                self.__thread.start()
            i = 0
            for i, task in enumerate(self.__task_list):
                if not task.done():
                    break
            self.__task_list = self.__task_list[i:]
        return result

    def finish_tasks(self):
        sleep_time = sys.getswitchinterval()
        shutdown_start_time = time.time()
        with self.__task_mutex:
            for task in self.__task_list:
                task_start_time = time.time()
                while not task.done() and (time.time() - task_start_time < DEFAULT_TASK_TIMEOUT) and (
                        time.time() - shutdown_start_time < DEFAULT_SHUTDOWN_TIMEOUT):
                    time.sleep(sleep_time)
                if time.time() - shutdown_start_time >= DEFAULT_SHUTDOWN_TIMEOUT:
                    break
            self.__task_list = []

    def clone(self) -> 'ThreadedRPClient':
        """Clone the client object, set current Item ID as cloned item ID.

        :returns: Cloned client object
        :rtype: ThreadedRPClient
        """
        cloned_client = self.__client.clone()
        # noinspection PyTypeChecker
        cloned = ThreadedRPClient(
            endpoint=None,
            project=None,
            launch_uuid=self.launch_uuid,
            client=cloned_client,
            loop=self.__loop
        )
        current_item = self.current_item()
        if current_item:
            cloned._add_current_item(current_item)
        return cloned


class BatchedRPClient(_SyncRPClient):
    __loop: asyncio.AbstractEventLoop
    __task_list: List[Task[_T]]
    __task_mutex: threading.Lock
    __last_run_time: float
    __thread: threading.Thread
    __trigger_num: int
    __trigger_interval: float

    def __init__(self, endpoint: str, project: str, *, launch_uuid: Optional[Task[str]] = None,
                 client: Optional[Client] = None, trigger_num: int = DEFAULT_TASK_TRIGGER_NUM,
                 trigger_interval: float = DEFAULT_TASK_TRIGGER_INTERVAL, **kwargs: Any) -> None:
        super().__init__(endpoint, project, launch_uuid=launch_uuid, client=client, **kwargs)

        self.__task_list = []
        self.__task_mutex = threading.Lock()
        self.__last_run_time = time.time()
        self.__loop = asyncio.new_event_loop()
        self.__thread = threading.current_thread()
        self.__loop.set_task_factory(BatchedTaskFactory(self.__loop, self.__thread))
        self.__trigger_num = trigger_num
        self.__trigger_interval = trigger_interval

    def __ready_to_run(self) -> bool:
        current_time = time.time()
        last_time = self.__last_run_time
        if len(self.__task_list) <= 0:
            return False
        if (len(self.__task_list) >= self.__trigger_num
                or current_time - last_time >= self.__trigger_interval):
            self.__last_run_time = current_time
            return True
        return False

    def create_task(self, coro: Coroutine[Any, Any, _T]) -> Task[_T]:
        result = self.__loop.create_task(coro)
        tasks = None
        with self.__task_mutex:
            self.__task_list.append(result)
            if self.__ready_to_run():
                tasks = self.__task_list
                self.__task_list = []
        if tasks:
            self.__loop.run_until_complete(asyncio.gather(*tasks))
        return result

    def finish_tasks(self) -> None:
        tasks = None
        with self.__task_mutex:
            if len(self.__task_list) > 0:
                tasks = self.__task_list
                self.__task_list = []
        if tasks:
            self.__loop.run_until_complete(asyncio.gather(*tasks))

    def clone(self) -> 'BatchedRPClient':
        """Clone the client object, set current Item ID as cloned item ID.

        :returns: Cloned client object
        :rtype: BatchedRPClient
        """
        cloned_client = self.__client.clone()
        # noinspection PyTypeChecker
        cloned = BatchedRPClient(
            endpoint=None,
            project=None,
            launch_uuid=self.launch_uuid,
            client=cloned_client,
            loop=self.__loop
        )
        current_item = self.current_item()
        if current_item:
            cloned._add_current_item(current_item)
        return cloned
