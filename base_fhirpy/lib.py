import json
import copy
from abc import ABC, abstractmethod
from collections import defaultdict

import requests


from .utils import (
    encode_params, convert_values, get_by_path, parse_path, chunks)
from .exceptions import (
    ResourceNotFound, OperationOutcome, InvalidResponse)


class Client(ABC):
    schema = None
    resources_cache = None
    url = None
    authorization = None
    without_cache = False

    def __init__(self, url, authorization=None, with_cache=False,
                 schema=None):
        self.url = url
        self.authorization = authorization
        self.resources_cache = defaultdict(dict)
        self.without_cache = not with_cache
        if schema:
            self.schema = schema

    def __str__(self):  # pragma: no cover
        return '<{0} {1}>'.format(self.__class__.__name__, self.url)

    def __repr__(self):  # pragma: no cover
        return self.__str__()

    @property
    @abstractmethod
    def searchset_class(self):
        pass

    @property
    @abstractmethod
    def resource_class(self):
        pass

    def _add_resource_to_cache(self, resource):
        if self.without_cache:
            return

        self.resources_cache[resource.resource_type][resource.id] = resource

    def _remove_resource_from_cache(self, resource):
        if self.without_cache:
            return

        del self.resources_cache[resource.resource_type][resource.id]

    def _get_resource_from_cache(self, resource_type, id):
        if self.without_cache:
            return None

        return self.resources_cache[resource_type].get(id, None)

    def clear_resources_cache(self, resource_type=None):
        if self.without_cache:
            return

        if resource_type:
            self.resources_cache[resource_type] = {}
        else:
            self.resources_cache = defaultdict(dict)

    @abstractmethod
    def reference(self, resource_type=None, id=None, reference=None, **kwargs):
        pass

    def resource(self, resource_type=None, **kwargs):
        if resource_type is None:
            raise TypeError('Argument `resource_type` is required')

        return self.resource_class(
            self,
            resource_type=resource_type,
            **kwargs
        )

    def resources(self, resource_type):
        return self.searchset_class(self, resource_type=resource_type)

    def _do_request(self, method, path, data=None, params=None):
        params = params or {}
        params.update({'_format': 'json'})
        url = '{0}/{1}?{2}'.format(
            self.url, path, encode_params(params))

        r = requests.request(
            method,
            url,
            json=data,
            headers={'Authorization': self.authorization})

        if 200 <= r.status_code < 300:
            return json.loads(r.content) if r.content else None

        if r.status_code == 404:
            raise ResourceNotFound(r.content.decode())

        raise OperationOutcome(r.content.decode())

    def _fetch_resource(self, path, params=None):
        return self._do_request('get', path, params=params)

    def _get_schema(self):
        return self.schema


class SearchSet(ABC):
    client = None
    resource_type = None
    params = None

    def __init__(self, client, resource_type, params=None):
        self.client = client
        self.resource_type = resource_type
        self.params = defaultdict(list, params or {})

    def _perform_resource(self, data, skip_caching):
        resource_type = data.get('resourceType', None)
        resource = self.client.resource(resource_type, **data)

        if not skip_caching:
            self.client._add_resource_to_cache(resource)

        return resource

    def fetch(self, *, skip_caching=False):
        bundle_data = self.client._fetch_resource(
            self.resource_type, self.params)
        bundle_resource_type = bundle_data.get('resourceType', None)

        if bundle_resource_type != 'Bundle':
            raise InvalidResponse(
                'Expected to receive Bundle '
                'but {0} received'.format(bundle_resource_type))

        resources_data = [
            res['resource'] for res in bundle_data.get('entry', [])]

        resources = []
        for data in resources_data:
            resource = self._perform_resource(data, skip_caching)
            if resource.resource_type == self.resource_type:
                resources.append(resource)

        return resources

    def fetch_all(self, *, skip_caching=False):
        page = 1
        resources = []

        while True:
            new_resources = self.page(page).fetch(skip_caching=skip_caching)
            if not new_resources:
                break

            resources.extend(new_resources)
            page += 1

        return resources

    def get(self, id, *, skip_caching=False):
        res_data = self.client._fetch_resource(
            '{0}/{1}'.format(self.resource_type, id))

        if res_data['resourceType'] != self.resource_type:
            raise InvalidResponse(
                'Expected to receive {0} '
                'but {1} received'.format(self.resource_type,
                                          res_data['resourceType']))

        return self._perform_resource(res_data, skip_caching)

    def count(self):
        new_params = copy.deepcopy(self.params)
        new_params['_count'] = 1
        new_params['_totalMethod'] = 'count'

        return self.client._fetch_resource(
            self.resource_type,
            params=new_params
        )['total']

    def first(self):
        result = self.limit(1).fetch()

        return result[0] if result else None

    def clone(self, override=False, **kwargs):
        new_params = copy.deepcopy(self.params)
        for key, value in kwargs.items():
            if not isinstance(value, list):
                value = [value]

            if override:
                new_params[key] = value
            else:
                new_params[key].extend(value)

        return self.__class__(self.client, self.resource_type, new_params)

    def elements(self, *attrs, exclude=False):
        attrs = set(attrs)
        if not exclude:
            attrs |= {'id', 'resourceType'}
        attrs = [attr for attr in attrs]

        return self.clone(
            _elements='{0}{1}'.format('-' if exclude else '',
                                      ','.join(attrs)),
            override=True
        )

    def include(self, resource_type, attr, target_resource_type=None,
                *, recursive=False):
        key_params = ['_include']
        if recursive:
            key_params.append('recursive')
        key = ':'.join(key_params)

        value_params = [resource_type, attr]
        if target_resource_type:
            value_params.append(target_resource_type)
        value = ':'.join(value_params)

        return self.clone(**{key: value})

    def has(self, *args, **kwargs):
        if len(args) % 2 != 0:
            raise TypeError(
                'You should pass even size of arguments, for example: '
                '`.has(\'Observation\', \'patient\', '
                '\'AuditEvent\', \'entity\', user=\'id\')`')

        key_part = ':'.join(
            ['_has:{0}'.format(':'.join(pair))
             for pair in chunks(args, 2)])

        return self.clone(
            **{':'.join([key_part, key]): value
               for key, value in kwargs.items()})

    def revinclude(self, resource_type, attr, recursive=False):
        # For the moment, this method might only have useless behaviour
        # because you don't have any possibilities to access the related data

        raise NotImplementedError()

    def search(self, **kwargs):
        return self.clone(**kwargs)

    def limit(self, limit):
        return self.clone(_count=limit, override=True)

    def page(self, page):
        return self.clone(page=page, override=True)

    def sort(self, *keys):
        sort_keys = ','.join(keys)
        return self.clone(_sort=sort_keys, override=True)

    def __str__(self):  # pragma: no cover
        return '<{0} {1}?{2}>'.format(
            self.__class__.__name__,
            self.resource_type,
            encode_params(self.params))

    def __repr__(self):  # pragma: no cover
        return self.__str__()

    def __iter__(self):
        return iter(self.fetch())


class AbstractResource(dict):
    client = None

    def __init__(self, client, **kwargs):
        self.client = client

        self._raise_error_if_invalid_keys(kwargs.keys())
        super(AbstractResource, self).__init__(**kwargs)

    def __eq__(self, other):
        return isinstance(other, AbstractResource) \
               and self.reference == other.reference

    def __setitem__(self, key, value):
        self._raise_error_if_invalid_key(key)

        super(AbstractResource, self).__setitem__(key, value)

    def __getitem__(self, key):
        self._raise_error_if_invalid_key(key)

        return super(AbstractResource, self).__getitem__(key)

    def get_by_path(self, path, default=None):
        keys = parse_path(path)

        self._raise_error_if_invalid_key(keys[0])

        return get_by_path(self, keys, default)

    def get(self, key, default=None):
        self._raise_error_if_invalid_key(key)

        return super(AbstractResource, self).get(key, default)

    def setdefault(self, key, default=None):
        self._raise_error_if_invalid_key(key)

        return super(AbstractResource, self).setdefault(key, default)

    def serialize(self):
        def convert_fn(item):
            if isinstance(item, Resource):
                return item.to_reference().serialize(), True
            elif isinstance(item, Reference):
                return item.serialize(), True
            else:
                return item, False

        return convert_values(
            {key: value for key, value in self.items()}, convert_fn)

    def get_root_keys(self):  # pragma: no cover
        raise NotImplementedError

    @property
    def id(self):  # pragma: no cover
        raise NotImplementedError()

    @property
    def resource_type(self):  # pragma: no cover
        raise NotImplementedError()

    @property
    def reference(self):  # pragma: no cover
        raise NotImplementedError()

    def _ipython_key_completions_(self):  # pragma: no cover
        return self.get_root_keys()

    def _raise_error_if_invalid_keys(self, keys):
        schema = self.client._get_schema()
        if not schema:
            return
        root_attrs = self.get_root_keys()

        for key in keys:
            if key not in root_attrs:
                raise KeyError(
                    'Invalid key `{0}`. Possible keys are `{1}`'.format(
                        key, ', '.join(root_attrs)))

    def _raise_error_if_invalid_key(self, key):
        self._raise_error_if_invalid_keys([key])


class Resource(AbstractResource, ABC):
    resource_type = None

    def __init__(self, client, resource_type, **kwargs):
        def convert_fn(item):
            if isinstance(item, AbstractResource):
                return item, True

            if self.is_reference(item):
                return client.reference(**item), True

            return item, False

        self.resource_type = resource_type
        kwargs['resourceType'] = resource_type
        converted_kwargs = convert_values(kwargs, convert_fn)

        super(Resource, self).__init__(client, **converted_kwargs)

    def __setitem__(self, key, value):
        if key == 'resourceType' and 'resourceType' not in self:
            raise KeyError(
                'Can not change `resourceType` after instantiating resource. '
                'You must re-instantiate resource using '
                '`Client.resource` method')

        super(Resource, self).__setitem__(key, value)

    def __str__(self):  # pragma: no cover
        return '<{0} {1}>'.format(self.__class__.__name__, self._get_path())

    def __repr__(self):  # pragma: no cover
        return self.__str__()

    def get_root_keys(self):
        schema = self.client._get_schema()
        if not schema:
            return set()
        return set(schema.get(self.resource_type, [])) | \
               {'resourceType', 'id', 'meta', 'extension'}

    def save(self):
        data = self.client._do_request(
            'put' if self.id else 'post', 
            self._get_path(), 
            data=self.serialize())

        self['meta'] = data.get('meta', {})
        self['id'] = data.get('id')

        self.client._add_resource_to_cache(self)

    def delete(self):
        self.client._remove_resource_from_cache(self)

        return self.client._do_request('delete', self._get_path())

    def to_resource(self, nocache=False):
        """
        Returns Resource instance for this resource
        """
        return self

    def to_reference(self, **kwargs):
        """
        Returns Reference instance for this resource
        """
        if not self.reference:
            raise ResourceNotFound(
                'Can not get reference to unsaved resource without id')

        return self.client.reference(reference=self.reference, **kwargs)

    @abstractmethod
    def is_reference(self, value):
        pass

    @property
    def id(self):
        return self.get('id', None)

    @property
    def reference(self):
        """
        Returns reference if local resource is saved
        """
        if self.id:
            return '{0}/{1}'.format(self.resource_type, self.id)

    def _get_path(self):
        if self.id:
            return '{0}/{1}'.format(self.resource_type, self.id)
        elif self.resource_type == 'Bundle':
            return ''

        return self.resource_type


class Reference(AbstractResource):
    def __str__(self):  # pragma: no cover
        return '<{0} {1}>'.format(self.__class__.__name__, self.reference)

    def __repr__(self):  # pragma: no cover
        return self.__str__()

    def to_resource(self, nocache=False):
        """
        Returns Resource instance for this reference from cache
        if nocache is not specified and from fhir server otherwise.
        """
        if not self.is_local:
            raise ResourceNotFound(
                'Can not resolve not local resource')

        cached_resource = self.client._get_resource_from_cache(
            self.resource_type, self.id)

        if cached_resource and not nocache:
            return cached_resource

        return self.client.resources(self.resource_type).get(self.id)

    def to_reference(self, **kwargs):
        """
        Returns Reference instance for this reference
        """
        return self.client.reference(reference=self.reference, **kwargs)

    @abstractmethod
    def get_root_keys(self):
        pass

    @property
    @abstractmethod
    def reference(self):
        pass

    @property
    @abstractmethod
    def id(self):
        """
        Returns id if reference specifies to the local resource
        """
        pass

    @property
    @abstractmethod
    def resource_type(self):
        """
        Returns resource type if reference specifies to the local resource
        """
        pass

    @property
    @abstractmethod
    def is_local(self):
        pass
