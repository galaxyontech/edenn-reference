from __future__ import annotations

from typing import Any, Mapping, Optional, Self


class EdennError(Exception):
    """
    Shared base exception for Edenn runtime errors.

    The class is intentionally generic so provider, workflow, deployment, and
    media layers can all extend it without depending on each other.
    """

    default_error_code = "edenn_error"
    default_public_message: Optional[str] = None

    def __init__(
        self,
        message: str,
        *,
        public_message: Optional[str] = None,
        error_code: Optional[str] = None,
        component: Optional[str] = None,
        operation: Optional[str] = None,
        retryable: bool = False,
        status_code: Optional[int] = None,
        context: Optional[Mapping[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.message = str(message)
        self.public_message = (
            str(public_message)
            if public_message is not None
            else (self.default_public_message or self.message)
        )
        self.error_code = error_code or self.default_error_code
        self.component = component
        self.operation = operation
        self.retryable = bool(retryable)
        self.status_code = status_code
        self.context = dict(context or {})
        self.cause = cause
        super().__init__(self.message)

    @classmethod
    def wrap(
        cls,
        exc: BaseException,
        *,
        message: Optional[str] = None,
        **kwargs: Any,
    ) -> Self:
        """
        Wrap an existing exception while preserving the original cause.
        """
        return cls(message or str(exc) or cls.__name__, cause=exc, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.__class__.__name__,
            "error_code": self.error_code,
            "message": self.message,
            "public_message": self.public_message,
            "retryable": self.retryable,
        }
        if self.component:
            payload["component"] = self.component
        if self.operation:
            payload["operation"] = self.operation
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.context:
            payload["context"] = dict(self.context)
        if self.cause is not None:
            payload["cause_type"] = type(self.cause).__name__
            payload["cause_message"] = str(self.cause)
        return payload

    def __str__(self) -> str:
        return f"[{self.error_code}] {self.message}"


class EdennConfigurationError(EdennError):
    default_error_code = "configuration_error"
    default_public_message = "The service is temporarily unavailable. Please try again later."


class EdennValidationError(EdennError):
    default_error_code = "validation_error"
    default_public_message = "The request could not be processed. Please review the input and try again."


class EdennUnsafeAssetUrlError(EdennValidationError):
    """A caller-supplied URL that points somewhere we refuse to fetch from.

    A validation error, not a server fault: the request named an address on our
    own network (loopback, private range, or the cloud metadata service), and
    the answer is to tell the caller their URL is unusable — never to make the
    request and find out.
    """

    default_error_code = "unsafe_asset_url"
    default_public_message = (
        "The asset URL must be a public http or https address. Private, "
        "loopback and link-local addresses are not fetched."
    )


class EdennInputVideoTooLargeError(EdennValidationError):
    default_error_code = "input_video_too_large"
    default_public_message = "The source video is too large. Please provide a video no larger than 300MB."


class EdennInputVideoTooLongError(EdennValidationError):
    default_error_code = "input_video_too_long"
    default_public_message = "The source video is too long. Please provide a video no longer than 150 seconds."


class EdennInputVideoTooShortError(EdennValidationError):
    default_error_code = "input_video_too_short"
    default_public_message = "The source video is too short. Please provide a video longer than 15 seconds."


class EdennImageDurationTooShortError(EdennValidationError):
    default_error_code = "image_duration_too_short"
    default_public_message = "Each image must be displayed for at least 3 seconds."


class EdennLyricsUnsupportedModelspecError(EdennValidationError):
    default_error_code = "lyrics_unsupported_modelspec"
    default_public_message = (
        "Lyric direction is not supported by this model specification. "
        "To use lyrics_prompt, set modelspec=edenn_enhanced or modelspec=edenn_studio."
    )


class EdennContentPolicyViolationError(EdennValidationError):
    default_error_code = "content_policy_violation"
    default_public_message = "The request could not be processed because it triggered content safety checks. Please modify the prompt and try again."

    def __init__(
        self,
        message: str,
        *,
        provider_name: Optional[str] = None,
        policy_code: Optional[str] = None,
        provider_error_code: Optional[str] = None,
        param: Optional[str] = None,
        filter_results: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        self.provider_name = provider_name
        self.policy_code = policy_code
        self.provider_error_code = provider_error_code
        self.param = param
        self.filter_results = dict(filter_results or {})
        component = kwargs.pop("component", None) or provider_name
        super().__init__(message, component=component, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.provider_name:
            payload["provider_name"] = self.provider_name
        if self.policy_code:
            payload["policy_code"] = self.policy_code
        if self.provider_error_code:
            payload["provider_error_code"] = self.provider_error_code
        if self.param:
            payload["param"] = self.param
        if self.filter_results:
            payload["filter_results"] = dict(self.filter_results)
        return payload


class EdennDeploymentError(EdennError):
    default_error_code = "deployment_error"
    default_public_message = "The request could not be completed due to a service error. Please try again later."


class EdennApiError(EdennDeploymentError):
    default_error_code = "api_error"


class EdennWorkflowError(EdennError):
    default_error_code = "workflow_error"
    default_public_message = "The request could not be completed. Please try again later."


class EdennStageError(EdennWorkflowError):
    default_error_code = "stage_error"

    def __init__(
        self,
        message: str,
        *,
        stage_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.stage_name = stage_name
        component = kwargs.pop("component", None) or stage_name
        super().__init__(message, component=component, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.stage_name:
            payload["stage_name"] = self.stage_name
        return payload


class EdennProviderError(EdennError):
    default_error_code = "provider_error"
    default_public_message = "An upstream generation service failed. Please try again later."

    def __init__(
        self,
        message: str,
        *,
        provider_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.provider_name = provider_name
        component = kwargs.pop("component", None) or provider_name
        super().__init__(message, component=component, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.provider_name:
            payload["provider_name"] = self.provider_name
        return payload


class EdennProviderAuthenticationError(EdennProviderError):
    default_error_code = "provider_authentication_error"
    default_public_message = "An upstream generation service is temporarily unavailable. Please try again later."


class EdennProviderRateLimitError(EdennProviderError):
    default_error_code = "provider_rate_limit_error"
    default_public_message = "The generation service is busy right now. Please try again shortly."


class EdennProviderTimeoutError(EdennProviderError):
    default_error_code = "provider_timeout_error"
    default_public_message = "The request took too long to complete. Please try again."


class EdennProviderImageFetchTimeoutError(EdennProviderTimeoutError):
    default_error_code = "provider_image_fetch_timeout"
    default_public_message = "The generation service timed out while reading an input image. Please try again."

    def __init__(
        self,
        message: str,
        *,
        failed_image_url: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.failed_image_url = failed_image_url
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.failed_image_url:
            payload["failed_image_url"] = self._redact_url_query(self.failed_image_url)
        return payload

    @staticmethod
    def _redact_url_query(url: str) -> str:
        base, sep, _query = url.partition("?")
        if not sep:
            return url
        return f"{base}?REDACTED"


class EdennProviderResponseError(EdennProviderError):
    default_error_code = "provider_response_error"
    default_public_message = "The generation service returned an unexpected response. Please try again later."


class EdennMediaProcessingError(EdennError):
    default_error_code = "media_processing_error"
    default_public_message = "The media could not be processed. Please try a different file or try again later."


class EdennStorageError(EdennError):
    default_error_code = "storage_error"
    default_public_message = "The generated assets could not be stored. Please try again later."


class EdennCallbackError(EdennError):
    default_error_code = "callback_error"
    default_public_message = "A callback processing error occurred. Please try again later."


__all__ = [
    "EdennError",
    "EdennConfigurationError",
    "EdennValidationError",
    "EdennInputVideoTooLargeError",
    "EdennInputVideoTooLongError",
    "EdennInputVideoTooShortError",
    "EdennContentPolicyViolationError",
    "EdennDeploymentError",
    "EdennApiError",
    "EdennWorkflowError",
    "EdennStageError",
    "EdennProviderError",
    "EdennProviderAuthenticationError",
    "EdennProviderRateLimitError",
    "EdennProviderTimeoutError",
    "EdennProviderImageFetchTimeoutError",
    "EdennProviderResponseError",
    "EdennMediaProcessingError",
    "EdennStorageError",
    "EdennCallbackError",
]
