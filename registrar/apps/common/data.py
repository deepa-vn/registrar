"""
Module for syncing data with external services.
"""
import logging
from collections import namedtuple
from datetime import datetime
from posixpath import join as urljoin

from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from edx_rest_api_client import client as rest_client
from requests.exceptions import HTTPError
from rest_framework.status import HTTP_404_NOT_FOUND

from registrar.apps.common.constants import (
    PROGRAM_CACHE_KEY_TPL,
    PROGRAM_CACHE_TIMEOUT,
)


logger = logging.getLogger(__name__)
DISCOVERY_PROGRAM_API_TPL = 'api/v1/programs/{}/'

DiscoveryCourseRun = namedtuple(
    'DiscoveryCourseRun',
    ['key', 'external_key', 'title', 'marketing_url'],
)


class DiscoveryProgram(object):
    """
    Data about a program from Course Discovery service.

    Is loaded from Discovery service and cached indefinitely until invalidated.

    Attributes:
        * version (int)
        * loaded (datetime): When data was loaded from Course Discovery
        * uuid (str): Program UUID-4
        * title (str): Program title
        * url (str): Program marketing-url
        * active_curriculum_uuid (str): UUID-4 of active curriculum.
        * course_runs (list[DiscoveryCourseRun]):
            Flattened list of all course runs in program
    """

    # If we change the schema of this class, bump the `class_version`
    # so that all old entries will be ignored.
    class_version = 1

    def __init__(self, **kwargs):
        self.loaded = datetime.now()
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def get(cls, program_uuid, client=None):
        """
        Get a DiscoveryProgram instance, either by loading it from the cache,
        or query the Course Discovery service if it is not in the cache.

        Raises Http404 if program is not cached and Discovery returns 404
        Raises HTTPError if program is not cached and Discover returns error.
        Raises ValidationError if program is not cached and Discovery returns
            data in a format we don't like.
        """
        key = PROGRAM_CACHE_KEY_TPL.format(uuid=program_uuid)
        program = cache.get(key)
        if not (program and program.version == cls.class_version):
            program = cls.load_from_discovery(program_uuid, client)
            cache.set(key, program, PROGRAM_CACHE_TIMEOUT)
        return program

    @classmethod
    def read_from_discovery(cls, program_uuid, client=None):
        """
        Reads the json representation of a program from the Course Discovery service.

        Raises Http404 if program is not cached and Discovery returns 404
        Raises HTTPError if Discovery returns error.
        """
        url = urljoin(
            settings.DISCOVERY_BASE_URL, 'api/v1/programs/{}/'
        ).format(
            program_uuid
        )
        try:
            program_data = _make_request('GET', url, client).json()
        except HTTPError as e:
            if e.response.status_code == HTTP_404_NOT_FOUND:
                raise Http404(e)
            else:
                raise e
        return program_data

    @classmethod
    def load_from_discovery(cls, program_uuid, client=None):
        """
        Load a DiscoveryProgram instance from the Course Discovery service.

        Raises Http404 if program is not cached and Discovery returns 404
        Raises HTTPError if program is not cached AND Discovery returns error.
        """
        program_data = cls.read_from_discovery(program_uuid, client)
        return cls.from_json(program_uuid, program_data)

    @classmethod
    def from_json(cls, program_uuid, program_data):
        """
        Builds a DiscoveryProgram instance from JSON data that has been
        returned by the Course Discovery service.json
        """
        program_title = program_data.get('title')
        program_url = program_data.get('marketing_url')
        # this make two temporary assumptions (zwh 03/19)
        #  1. one *active* curriculum per program
        #  2. no programs are nested within a curriculum
        try:
            curriculum = next(
                c for c in program_data.get('curricula', [])
                if c.get('is_active')
            )
        except StopIteration:
            logger.exception(
                'Discovery API returned no active curricula for program {}'.format(
                    program_uuid
                )
            )
            return DiscoveryProgram(
                version=cls.class_version,
                uuid=program_uuid,
                title=program_title,
                url=program_url,
                active_curriculum_uuid=None,
                course_runs=[],
            )
        active_curriculum_uuid = curriculum.get("uuid")
        course_runs = [
            DiscoveryCourseRun(
                key=course_run.get('key'),
                external_key=course_run.get('external_key'),
                title=course_run.get('title'),
                marketing_url=course_run.get('marketing_url'),
            )
            for course in curriculum.get("courses", [])
            for course_run in course.get("course_runs", [])
        ]
        return DiscoveryProgram(
            version=cls.class_version,
            uuid=program_uuid,
            title=program_title,
            url=program_url,
            active_curriculum_uuid=active_curriculum_uuid,
            course_runs=course_runs,
        )

    def find_course_run(self, course_id):
        """
        Given a course id, return the course_run with that `key` or `external_key`

        Returns None if course run is not found in the cached program.
        """
        for course_run in self.course_runs:
            if course_id == course_run.key or course_id == course_run.external_key:
                return course_run
        return None

    def get_external_course_key(self, course_id):
        """
        Given a course ID, return the external course key for that course_run.
        The course key passed in may be an external or internal course key.


        Returns None if course run is not found in the cached program.
        """
        course_run = self.find_course_run(course_id)
        if course_run:
            return course_run.external_key
        return None

    def get_course_key(self, course_id):
        """
        Given a course ID, return the internal course ID for that course run.
        The course ID passed in may be an external or internal course key.

        Returns None if course run is not found in the cached program.
        """
        course_run = self.find_course_run(course_id)
        if course_run:
            return course_run.key
        return None


def _get_all_paginated_responses(url, client=None, expected_error_codes=None):
    """
    Builds a list of all responses from a cursor-paginated endpoint.

    Repeatedly performs request on 'next' URL until 'next' is null.

    Returns: list[HTTPResonse]
        A list of responses, returned in order of the requests made.
    """
    if not client:  # pragma: no branch
        client = _get_client(settings.LMS_BASE_URL)
    if not expected_error_codes:  # pragma: no cover
        expected_error_codes = set()
    responses = []
    next_url = url
    while next_url:
        try:
            response = _make_request('GET', next_url, client)
        except HTTPError as e:
            if e.response.status_code in expected_error_codes:
                response = e.response
            else:
                raise e
        responses.append(response)
        next_url = response.json().get('next')
    return responses


def _get_all_paginated_results(url, client=None, ):
    """
    Builds a list of all results from a cursor-paginated endpoint.

    Repeatedly performs request on 'next' URL until 'next' is null.
    """
    if not client:  # pragma: no branch
        client = _get_client(settings.LMS_BASE_URL)
    results = []
    next_url = url
    while next_url:
        response_data = _make_request('GET', next_url, client).json()
        results += response_data['results']
        next_url = response_data.get('next')
    return results


def _do_batched_lms_write(method, url, items, items_per_batch, client=None):
    """
    Make a series of requests to the LMS, each using a
    `items_per_batch`-sized chunk of the list `items` as input data.

    Returns: list[HTTPResonse]
        A list of responses, returned in order of the requests made.
    """
    client = client or _get_client(settings.LMS_BASE_URL)
    responses = []
    for i in range(0, len(items), items_per_batch):
        sub_items = items[i:(i + items_per_batch)]
        try:
            response = _make_request(method, url, client, json=sub_items)
        except HTTPError as e:
            response = e.response
        responses.append(response)
    return responses


def _make_request(method, url, client, **kwargs):
    """
    Helper method to make an http request using
    an authN'd client.
    """
    if method not in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']:  # pragma: no cover
        raise Exception('invalid http method: ' + method)

    if not client:
        client = _get_client(settings.LMS_BASE_URL)

    response = client.request(method, url, **kwargs)

    if response.status_code >= 200 and response.status_code < 300:
        return response
    else:
        response.raise_for_status()


def _get_client(host_base_url):
    """
    Returns an authenticated edX REST API client.
    """
    client = rest_client.OAuthAPIClient(
        host_base_url,
        settings.BACKEND_SERVICE_EDX_OAUTH2_KEY,
        settings.BACKEND_SERVICE_EDX_OAUTH2_SECRET,
    )
    client._check_auth()  # pylint: disable=protected-access
    if not client.auth.token:  # pragma: no cover
        raise 'No Auth Token'
    return client