import collections.abc as collections_abc
import inspect
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum, IntEnum
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type, Union
from typing import _GenericAlias as GenericAlias
from typing import get_type_hints
from uuid import UUID

from openapidocs.common import Format
from openapidocs.v3 import (
    Components,
    Example,
    Header,
    Info,
    MediaType,
    OpenAPI,
    Operation,
    Parameter,
    ParameterLocation,
    PathItem,
    Reference,
    RequestBody,
)
from openapidocs.v3 import Response as ResponseDoc
from openapidocs.v3 import Schema, Server, ValueFormat, ValueType

from blacksheep.server.bindings import (
    Binder,
    BodyBinder,
    CookieBinder,
    HeaderBinder,
    QueryBinder,
    RouteBinder,
    empty,
)
from blacksheep.server.openapi.docstrings import (
    DocstringInfo,
    get_handler_docstring_info,
)
from blacksheep.server.openapi.exceptions import (
    DuplicatedContentTypeDocsException,
    UnsupportedUnionTypeException,
)
from blacksheep.server.routing import Router

from ..application import Application
from .common import (
    APIDocsHandler,
    ContentInfo,
    EndpointDocs,
    HeaderInfo,
    ParameterInfo,
    ParameterSource,
    RequestBodyInfo,
    ResponseExample,
    ResponseInfo,
    ResponseStatusType,
    response_status_to_str,
)

try:
    from pydantic import BaseModel  # type: ignore
except ImportError:  # pragma: no cover
    # noqa
    BaseModel = ...  # type: ignore


def get_origin(object_type):
    return getattr(object_type, "__origin__", None)


def check_union(object_type: Any) -> Tuple[bool, Any]:
    if hasattr(object_type, "__origin__") and object_type.__origin__ is Union:
        # support only Union[None, Type] - that is equivalent of Optional[Type]
        if type(None) not in object_type.__args__ or len(object_type.__args__) > 2:
            raise UnsupportedUnionTypeException(object_type)

        for possible_type in object_type.__args__:
            if type(None) is possible_type:
                continue
            return True, possible_type
    return False, object_type


def is_ignored_parameter(param_name: str, matching_binder: Optional[Binder]) -> bool:
    # if a binder is used, handle only those that can be mapped to a type of
    # OpenAPI Documentation parameter's location
    if matching_binder and not isinstance(
        matching_binder,
        (
            QueryBinder,
            RouteBinder,
            HeaderBinder,
            CookieBinder,
        ),
    ):
        return True

    return param_name == "request" or param_name == "services"


@dataclass
class FieldInfo:
    name: str
    type: Union[Type, Schema]


class ObjectTypeHandler(ABC):
    """
    Abstract class describing the interface used to generate an OpenAPI Documentation
    schema for an object type.
    """

    @abstractmethod
    def handles_type(self, object_type) -> bool:
        """
        Returns a value indicating whether the given type is handled but this
        object type handler.
        """

    def get_type_fields(self, object_type) -> List[FieldInfo]:
        """
        Returns a set of fields to be used for the handled type.
        """


class DataClassTypeHandler(ObjectTypeHandler):
    def handles_type(self, object_type) -> bool:
        return is_dataclass(object_type)

    def get_type_fields(self, object_type) -> List[FieldInfo]:
        return [FieldInfo(field.name, field.type) for field in fields(object_type)]


class PydanticModelTypeHandler(ObjectTypeHandler):
    def handles_type(self, object_type) -> bool:
        if isinstance(object_type, GenericAlias):
            # support for Generic BaseModel here is better since it can extract items
            # type, skip this;
            # anyway using Generic with BaseModel doesn't seem to be recommended by
            # pydantic:
            # https://pydantic-docs.helpmanual.io/usage/types/#generic-classes-as-types
            return False
        if (
            BaseModel is not ...
            and inspect.isclass(object_type)
            and issubclass(object_type, BaseModel)  # type: ignore
        ):
            return True
        return False

    def _open_api_v2_field_schema_to_type(
        self, field_info, schema
    ) -> Union[Type, Schema]:
        if "$ref" in schema:
            return field_info.outer_type_

        value_type = schema.get("type")
        nullable = field_info.allow_none if field_info is not None else False

        if value_type == "string":
            return Schema(
                type=ValueType.STRING,
                min_length=schema.get("minLength"),
                max_length=schema.get("maxLength"),
                format=schema.get("format"),
                nullable=nullable,
            )

        if value_type == "boolean":
            return bool

        if value_type == "file":
            # TODO: improve support for file type,
            # see "Considerations for File Uploads"
            # at https://swagger.io/specification/
            return Schema(type=ValueType.STRING, format=ValueFormat.BINARY)

        if value_type == "number":
            return Schema(
                type=ValueType.NUMBER,
                minimum=schema.get("exclusiveMinimum"),
                maximum=schema.get("exclusiveMaximum"),
                format=ValueFormat.FLOAT,
                nullable=nullable,
            )

        if value_type == "integer":
            return Schema(
                type=ValueType.INTEGER,
                minimum=schema.get("exclusiveMinimum"),
                maximum=schema.get("exclusiveMaximum"),
                format=ValueFormat.INT64,
                nullable=nullable,
            )

        if value_type == "array":
            if field_info is not None:
                return field_info.outer_type_
            else:
                return list

        return Schema()

    def get_type_fields(self, object_type) -> List[FieldInfo]:
        # to support all pydantic special types, here we rely on its schema,
        # but since we want to be able to generate OpenAPI Documentation V3 (or V2 for
        # that matter), we extract basic information we need; also using the type's
        # __fields__ when available
        schema = object_type.schema()
        properties = schema["properties"]

        try:
            fields_info = object_type.__fields__
        except AttributeError:
            fields_info = dict()

        return [
            FieldInfo(
                name,
                self._open_api_v2_field_schema_to_type(
                    fields_info.get(name, None), value
                ),
            )
            for name, value in properties.items()
        ]


class OpenAPIHandler(APIDocsHandler[OpenAPI]):
    """
    Handles the automatic generation of OpenAPI Documentation, specification v3
    for a web application, exposing methods to enrich the documentation with details
    through decorators.
    """

    def __init__(
        self,
        *,
        info: Info,
        ui_path: str = "/docs",
        json_spec_path: str = "/openapi.json",
        yaml_spec_path: str = "/openapi.yaml",
        preferred_format: Format = Format.JSON,
        anonymous_access: bool = True,
    ) -> None:
        super().__init__(
            ui_path=ui_path,
            json_spec_path=json_spec_path,
            yaml_spec_path=yaml_spec_path,
            preferred_format=preferred_format,
            anonymous_access=anonymous_access,
        )
        self.info = info
        self.components = Components()
        self._objects_references: Dict[Any, Reference] = {}
        self.servers: List[Server] = []
        self.common_responses: Dict[ResponseStatusType, ResponseDoc] = {}
        self._object_types_handlers: List[ObjectTypeHandler] = [
            DataClassTypeHandler(),
            PydanticModelTypeHandler(),
        ]

    @property
    def object_types_handlers(self) -> List[ObjectTypeHandler]:
        return self._object_types_handlers

    def get_ui_page_title(self) -> str:
        return self.info.title

    def generate_documentation(self, app: Application) -> OpenAPI:
        return OpenAPI(
            info=self.info, paths=self.get_paths(app), components=self.components
        )

    def get_paths(self, app: Application) -> Dict[str, PathItem]:
        return self.get_routes_docs(app.router)

    def get_type_name_for_generic(
        self,
        object_type: GenericAlias,
        context_type_args: Optional[Dict[Any, Type]] = None,
    ) -> str:
        """
        This method returns a type name for a generic type.
        """
        assert isinstance(object_type, GenericAlias), "This method requires a generic"
        # Note: by default returns a string respectful of this requirement:
        # $ref values must be RFC3986-compliant percent-encoded URIs
        # Therefore, a generic that would be expressed in Python: Example[Foo, Bar]
        # and C# or TypeScript Example<Foo, Bar>
        # Becomes here represented as: ExampleOfFooAndBar
        origin = get_origin(object_type)
        args = object_type.__args__
        args_repr = "And".join(
            self.get_type_name(arg, context_type_args) for arg in args
        )
        return f"{self.get_type_name(origin)}Of{args_repr}"

    def get_type_name(
        self, object_type, context_type_args: Optional[Dict[Any, Type]] = None
    ) -> str:
        if context_type_args and object_type in context_type_args:
            object_type = context_type_args.get(object_type)
        if hasattr(object_type, "__name__"):
            return object_type.__name__
        if isinstance(object_type, GenericAlias):
            return self.get_type_name_for_generic(object_type, context_type_args)
        raise ValueError(
            f"Cannot obtain a name for object_type parameter: {object_type!r}"
        )

    def register_schema_for_type(self, object_type: Type) -> Reference:
        stored_ref = self._get_stored_reference(object_type, None)
        if stored_ref:
            return stored_ref

        schema = self.get_schema_by_type(object_type)
        if isinstance(schema, Schema):
            return self._register_schema(schema, self.get_type_name(object_type))
        return schema

    def _register_schema(self, schema: Schema, name: str) -> Reference:
        if self.components.schemas is None:
            self.components.schemas = {}

        if name in self.components.schemas:
            counter = 0
            base_name = name
            while name in self.components.schemas:
                counter += 1
                name = f"{base_name}{counter}"

        self.components.schemas[name] = schema
        return Reference(f"#/components/schemas/{name}")

    def _can_handle_class_type(self, object_type: Type) -> bool:
        return (
            any(
                handler.handles_type(object_type)
                for handler in self._object_types_handlers
            )
            or object_type in self._types_schemas
        )

    def _handle_type_having_explicit_schema(self, object_type: Type) -> Reference:
        schema = self._types_schemas[object_type]
        type_name = self.get_type_name(object_type, {})
        reference = self._register_schema(
            schema,
            type_name,
        )
        self._objects_references[object_type] = reference
        self._objects_references[type_name] = reference
        return reference

    def _get_schema_for_class(self, object_type: Type) -> Reference:
        assert self._can_handle_class_type(object_type)

        if object_type in self._types_schemas:
            return self._handle_type_having_explicit_schema(object_type)

        required: List[str] = []
        properties: Dict[str, Union[Schema, Reference]] = {}

        for field in self.get_fields(object_type):
            is_optional, child_type = check_union(field.type)
            if not is_optional:
                required.append(field.name)

            if isinstance(child_type, str):
                # this is a forward reference, we need to obtain a full type here
                annotations = get_type_hints(object_type)

                if field.name in annotations:
                    child_type = annotations[field.name]

            properties[field.name] = self.get_schema_by_type(child_type)

        return self._handle_object_type(object_type, properties, required)

    def _handle_object_type(
        self,
        object_type: Type,
        properties: Dict[str, Union[Schema, Reference]],
        required: List[str],
        context_type_args: Optional[Dict[Any, Type]] = None,
    ) -> Reference:
        type_name = self.get_type_name(object_type, context_type_args)
        reference = self._register_schema(
            Schema(
                type=ValueType.OBJECT,
                required=required or None,
                properties=properties,
            ),
            type_name,
        )
        self._objects_references[object_type] = reference
        self._objects_references[type_name] = reference
        return reference

    def _get_stored_reference(
        self, object_type: Type[Any], type_args: Optional[Dict[Any, Type]] = None
    ) -> Optional[Reference]:
        if object_type in self._objects_references:
            # if object_type is a generic, it can be like
            # Example[~T] while type_args can have the information: {~T: Foo}
            # in such case; check
            return self._objects_references[object_type]

        if type_args:
            type_name = self.get_type_name(object_type, type_args)
            if type_name in self._objects_references:
                return self._objects_references[type_name]

        return None

    def get_schema_by_type(
        self,
        object_type: Union[Type, Schema],
        type_args: Optional[Dict[Any, Type]] = None,
    ) -> Union[Schema, Reference]:
        if isinstance(object_type, Schema):
            return object_type

        stored_ref = self._get_stored_reference(object_type, type_args)
        if stored_ref:
            return stored_ref

        is_optional, child_type = check_union(object_type)
        schema = self._get_schema_by_type(child_type, type_args)
        if isinstance(schema, Schema) and not is_optional:
            schema.nullable = is_optional
        return schema

    def _get_schema_by_type(
        self, object_type: Type[Any], type_args: Optional[Dict[Any, Type]] = None
    ) -> Union[Schema, Reference]:
        stored_ref = self._get_stored_reference(object_type, type_args)
        if stored_ref:  # pragma: no cover
            return stored_ref

        if self._can_handle_class_type(object_type):
            return self._get_schema_for_class(object_type)

        schema = self._try_get_schema_for_simple_type(object_type)
        if schema:
            return schema

        # List, Set, Tuple are handled first than GenericAlias
        schema = self._try_get_schema_for_iterable(object_type, type_args)
        if schema:
            return schema

        if isinstance(object_type, GenericAlias):
            schema = self._try_get_schema_for_generic(object_type, type_args)
            if schema:
                return schema

        if inspect.isclass(object_type):
            schema = self._try_get_schema_for_enum(object_type)
        return schema or Schema()

    def _try_get_schema_for_simple_type(self, object_type: Type) -> Optional[Schema]:
        if object_type is str:
            return Schema(type=ValueType.STRING)

        if object_type is int:
            return Schema(type=ValueType.INTEGER, format=ValueFormat.INT64)

        if object_type is float:
            return Schema(type=ValueType.NUMBER, format=ValueFormat.FLOAT)

        if object_type is bool:
            return Schema(type=ValueType.BOOLEAN)

        if object_type is UUID:
            return Schema(type=ValueType.STRING, format=ValueFormat.UUID)

        if object_type is date:
            return Schema(type=ValueType.STRING, format=ValueFormat.DATE)

        if object_type is datetime:
            return Schema(type=ValueType.STRING, format=ValueFormat.DATETIME)

        return None

    def _try_get_schema_for_iterable(
        self, object_type: Type, context_type_args: Optional[Dict[Any, Type]] = None
    ) -> Optional[Schema]:
        if object_type in {list, set, tuple}:
            # the user didn't specify the item type
            return Schema(type=ValueType.ARRAY, items=Schema(type=ValueType.STRING))

        origin = get_origin(object_type)

        if not origin or origin not in {list, set, tuple, collections_abc.Sequence}:
            return None

        # can be List, List[str] or list[str] (Python 3.9),
        # note: it could also be union if it wasn't handled above for dataclasses
        try:
            type_args = object_type.__args__  # type: ignore
        except AttributeError:  # pragma: no cover
            item_type = str
        else:
            item_type = next(iter(type_args), str)

        if context_type_args and item_type in context_type_args:
            item_type = context_type_args.get(item_type)

        return Schema(
            type=ValueType.ARRAY,
            items=self.get_schema_by_type(item_type, context_type_args),
        )

    def get_fields(self, object_type: Any) -> List[FieldInfo]:
        for handler in self._object_types_handlers:
            if handler.handles_type(object_type):
                return handler.get_type_fields(object_type)

        return []

    def _try_get_schema_for_generic(
        self, object_type: Type, context_type_args: Optional[Dict[Any, Type]] = None
    ) -> Optional[Reference]:
        origin = get_origin(object_type)

        required: List[str] = []
        properties: Dict[str, Union[Schema, Reference]] = {}

        args = object_type.__args__
        parameters = origin.__parameters__
        type_args = dict(zip(parameters, args))

        for field in self.get_fields(origin):
            is_optional, child_type = check_union(field.type)
            if not is_optional:
                required.append(field.name)

            if isinstance(child_type, str):
                warnings.warn(
                    f"The return type {object_type!r} contains a forward reference "
                    f"for {field.name}. Forward references in Generic types are not "
                    "supported for automatic generation of OpenAPI Documentation. "
                    "For more information see "
                    "https://www.neoteroi.dev/blacksheep/openapi/",
                    UserWarning,
                )
                return None

            if child_type in type_args:
                # example:
                # class Foo(Generic[T]):
                #    item: T
                child_type = type_args.get(child_type)

            properties[field.name] = self.get_schema_by_type(child_type, type_args)

        return self._handle_object_type(
            object_type, properties, required, context_type_args
        )

    def _try_get_schema_for_enum(self, object_type: Type) -> Optional[Schema]:
        if issubclass(object_type, IntEnum):
            return Schema(type=ValueType.INTEGER, enum=[v.value for v in object_type])
        if issubclass(object_type, Enum):
            return Schema(type=ValueType.STRING, enum=[v.value for v in object_type])
        return None

    def _get_body_binder(self, handler: Any) -> Optional[BodyBinder]:
        return next(
            (binder for binder in handler.binders if isinstance(binder, BodyBinder)),
            None,
        )

    def _get_binder_by_name(self, handler: Any, name: str) -> Optional[Binder]:
        return next(
            (binder for binder in handler.binders if binder.parameter_name == name),
            None,
        )

    def get_request_body(self, handler: Any) -> Union[None, RequestBody, Reference]:
        if not hasattr(handler, "binders"):
            return None
        body_binder = self._get_body_binder(handler)

        if body_binder is None:
            return None

        docs = self.get_handler_docs(handler)
        body_info = docs.request_body if docs else None

        body_examples: Optional[Dict[str, Union[Example, Reference]]] = (
            {key: Example(value=value) for key, value in body_info.examples.items()}
            if body_info and body_info.examples
            else None
        )

        return RequestBody(
            content={
                body_binder.content_type: MediaType(
                    schema=self.get_schema_by_type(body_binder.expected_type),
                    examples=body_examples,
                )
            },
            required=body_binder.required,
            description=body_info.description if body_info else None,
        )

    def get_parameter_location_for_binder(
        self, binder: Binder
    ) -> Optional[ParameterLocation]:
        if isinstance(binder, RouteBinder):
            return ParameterLocation.PATH
        if isinstance(binder, QueryBinder):
            return ParameterLocation.QUERY
        if isinstance(binder, CookieBinder):
            return ParameterLocation.COOKIE
        if isinstance(binder, HeaderBinder):
            return ParameterLocation.HEADER
        return None

    def _parameter_source_to_openapi_obj(
        self, value: ParameterSource
    ) -> ParameterLocation:
        return ParameterLocation[value.value.upper()]

    def get_parameters(
        self, handler: Any
    ) -> Optional[List[Union[Parameter, Reference]]]:
        if not hasattr(handler, "binders"):
            return None
        binders: List[Binder] = handler.binders
        parameters: Mapping[str, Union[Parameter, Reference]] = {}

        docs = self.get_handler_docs(handler)
        parameters_info = (docs.parameters if docs else None) or dict()

        for binder in binders:
            location = self.get_parameter_location_for_binder(binder)

            if not location:
                # the binder is used for something that is not a parameter
                # expressed in OpenAPI Docs (e.g. a DI service)
                continue

            if location == ParameterLocation.PATH:
                required = True
            else:
                required = binder.required and binder.default is empty

            # did the user specified information about the parameter?
            param_info = parameters_info.get(binder.parameter_name)

            parameters[binder.parameter_name] = Parameter(
                name=binder.parameter_name,
                in_=location,
                required=required or None,
                schema=self.get_schema_by_type(binder.expected_type),
                description=param_info.description if param_info else "",
                example=param_info.example if param_info else None,
            )

        for key, param_info in parameters_info.items():
            if key not in parameters:
                parameters[key] = Parameter(
                    name=key,
                    in_=self._parameter_source_to_openapi_obj(
                        param_info.source or ParameterSource.QUERY
                    ),
                    required=param_info.required,
                    schema=self.get_schema_by_type(param_info.value_type)
                    if param_info.value_type
                    else None,
                    description=param_info.description,
                    example=param_info.example,
                )

        return list(parameters.values())

    def _get_media_type_from_content_doc(self, content_doc: ContentInfo) -> MediaType:
        media_type = MediaType()

        if content_doc.type:
            media_type.schema = self.get_schema_by_type(content_doc.type)

        examples = content_doc.examples
        if examples:
            if len(examples) == 1:
                example_item = examples[0]
                if isinstance(example_item, ResponseExample):
                    media_type.example = self.normalize_example(example_item.value)
                else:
                    media_type.example = self.normalize_example(example_item)
            else:
                examples_doc: Dict[str, Union[Example, Reference]] = {}

                for index, example in enumerate(examples):
                    # support something that is not a ResponseExample,
                    # to offer a more concise API
                    if not isinstance(example, ResponseExample):
                        example = ResponseExample(example)

                    if not example.name:
                        example.name = f"example {index}"
                    examples_doc[example.name] = Example(
                        summary=example.summary,
                        description=example.description,
                        value=self.normalize_example(example.value),
                    )
                media_type.examples = examples_doc

        return media_type

    def _get_content_from_response_info(
        self, response_content: Optional[List[ContentInfo]]
    ) -> Optional[Dict[str, Union[MediaType, Reference]]]:
        if not response_content:
            return None

        oad_content: Dict[str, Union[MediaType, Reference]] = {}

        for content in response_content:
            if content.content_type not in oad_content:
                oad_content[
                    content.content_type
                ] = self._get_media_type_from_content_doc(content)
            else:
                raise DuplicatedContentTypeDocsException(content.content_type)

        return oad_content

    def _get_headers_from_response_info(
        self, headers: Optional[Dict[str, HeaderInfo]]
    ) -> Optional[Dict[str, Union[Header, Reference]]]:
        if headers is None:
            return None

        return {
            key: Header(value.description, self.get_schema_by_type(value.type))
            for key, value in headers.items()
        }

    def get_responses(self, handler: Any) -> Optional[Dict[str, ResponseDoc]]:
        docs = self.get_handler_docs(handler)
        data = docs.responses if docs else None

        # common responses used by the whole application, like responses sent in case
        # of error
        responses = {
            response_status_to_str(key): value
            for key, value in self.common_responses.items()
        }

        if not data:
            # try to generate automatically from the handler's return type annotations
            return_type = getattr(handler, "return_type", ...)

            if return_type is not ...:
                # automatically set response content for status 200,
                # if the user wants major control, it's necessary to use the decorators
                # responses[response_status_to_str(200)] = return_type
                if data is None:
                    data = {}

                if return_type is None:
                    # the user explicitly marked the request handler as returning None,
                    # document therefore HTTP 204 No Content
                    data["204"] = ResponseInfo("Success response", content=[])
                else:
                    # if the return type is optional, generate automatically
                    # the "404" response
                    is_optional, child_type = check_union(return_type)

                    data["200"] = ResponseInfo(
                        "Success response",
                        content=[ContentInfo(child_type, examples=[])],
                    )

                    if is_optional and self.handle_optional_response_with_404:
                        data["404"] = ResponseInfo("Object not found")
            else:
                return responses

        responses.update(
            {
                response_status_to_str(key): (
                    ResponseDoc(
                        description=value.description,
                        content=self._get_content_from_response_info(value.content),
                        headers=self._get_headers_from_response_info(value.headers),
                    )
                    if isinstance(value, ResponseInfo)
                    else ResponseDoc(description=value)
                )
                for key, value in data.items()
            }
        )
        return responses

    def on_docs_generated(self, docs: OpenAPI) -> None:
        docs.servers = self.servers

    def _merge_documentation(
        self,
        handler,
        endpoint_docs: EndpointDocs,
        docstring_info: DocstringInfo,
    ) -> None:
        if not endpoint_docs.description and docstring_info.description:
            endpoint_docs.description = docstring_info.description

        if not endpoint_docs.summary and docstring_info.summary:
            endpoint_docs.summary = docstring_info.summary

        for param_name, param_info in docstring_info.parameters.items():
            if endpoint_docs.parameters is None:
                endpoint_docs.parameters = {}

            # did the user specify parameter information explicitly, using @docs?
            matching_parameter = endpoint_docs.parameters.get(param_name)

            if matching_parameter is None:
                matching_binder = self._get_binder_by_name(handler, param_name)

                if isinstance(matching_binder, BodyBinder):
                    if endpoint_docs.request_body is None:
                        endpoint_docs.request_body = RequestBodyInfo(
                            description=param_info.description
                        )
                    elif not endpoint_docs.request_body.description:
                        endpoint_docs.request_body.description = param_info.description
                    continue

                if is_ignored_parameter(param_name, matching_binder):
                    # this must not be documented in OpenAPI Documentation!
                    continue

                assert isinstance(endpoint_docs.parameters, dict)
                endpoint_docs.parameters[param_name] = ParameterInfo(
                    value_type=param_info.value_type,
                    required=param_info.required,
                    description=param_info.description,
                    source=param_info.source or ParameterSource.QUERY,
                )
            else:
                matching_parameter.description = param_info.description

                if (
                    matching_parameter.value_type is None
                    and param_info.value_type is not None
                ):
                    matching_parameter.value_type = param_info.value_type

    def _apply_docstring(self, handler, docs: Optional[EndpointDocs]) -> None:
        if not self.use_docstrings:  # pragma: no cover
            return
        docstring_info = get_handler_docstring_info(handler)

        if docstring_info is not None:
            if docs is None:
                docs = self.get_handler_docs_or_set(handler)
            self._merge_documentation(handler, docs, docstring_info)

    def get_operation_id(self, docs: Optional[EndpointDocs], handler) -> str:
        return handler.__name__

    def get_routes_docs(self, router: Router) -> Dict[str, PathItem]:
        """Obtains a documentation object from the routes defined in a router."""
        paths_doc: Dict[str, PathItem] = {}
        raw_dict = self.router_to_paths_dict(router, lambda route: route)

        for path, conf in raw_dict.items():
            path_item = PathItem()

            for method, route in conf.items():
                handler = self._get_request_handler(route)
                docs = self.get_handler_docs(handler)
                self._apply_docstring(handler, docs)

                operation = Operation(
                    description=self.get_description(handler),
                    summary=self.get_summary(handler),
                    responses=self.get_responses(handler) or {},
                    operation_id=self.get_operation_id(docs, handler),
                    parameters=self.get_parameters(handler),
                    request_body=self.get_request_body(handler),
                    deprecated=self.is_deprecated(handler),
                    tags=self.get_handler_tags(handler),
                )
                if docs and docs.on_created:
                    docs.on_created(self, operation)
                setattr(path_item, method, operation)

            paths_doc[path] = path_item

        return paths_doc
