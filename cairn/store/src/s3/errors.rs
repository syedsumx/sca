//! S3 error responses.

#[derive(Clone, Debug, PartialEq)]
pub struct ApiError {
    pub code: &'static str,
    pub message: &'static str,
    pub status: u16,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ApiError {}

macro_rules! errors {
    ($($name:ident = $code:literal, $status:literal, $msg:literal;)*) => {
        $(pub static $name: ApiError = ApiError { code: $code, message: $msg, status: $status };)*
    };
}

errors! {
    ACCESS_DENIED = "AccessDenied", 403, "Access Denied";
    AUTH_HEADER_MALFORMED = "AuthorizationHeaderMalformed", 400, "The authorization header is malformed";
    AUTH_QUERY_MALFORMED = "AuthorizationQueryParametersError", 400, "The presigned URL parameters are malformed";
    INVALID_ACCESS_KEY = "InvalidAccessKeyId", 403, "The access key ID you provided does not exist in our records.";
    SIGNATURE_MISMATCH = "SignatureDoesNotMatch", 403, "The request signature we calculated does not match the signature you provided.";
    EXPIRED = "AccessDenied", 403, "Request has expired";
    TIME_SKEWED = "RequestTimeTooSkewed", 403, "The difference between the request time and the server's time is too large.";
    MISSING_CONTENT_SHA = "InvalidRequest", 400, "Missing required header for this request: x-amz-content-sha256";
    CONTENT_SHA_INVALID = "InvalidArgument", 400, "x-amz-content-sha256 must be UNSIGNED-PAYLOAD, a streaming mode, or a valid sha256 value.";
    MISSING_DECODED_LENGTH = "MissingContentLength", 411, "x-amz-decoded-content-length is required for aws-chunked uploads";
    INVALID_DIGEST = "InvalidDigest", 400, "The Content-MD5 you specified is not valid.";
    BAD_DIGEST = "BadDigest", 400, "The Content-MD5 you specified did not match what we received.";
    CONTENT_SHA_MISMATCH = "XAmzContentSHA256Mismatch", 400, "The provided 'x-amz-content-sha256' header does not match what was computed.";
    CHECKSUM_MISMATCH = "BadDigest", 400, "The checksum you specified did not match the data received.";
    INCOMPLETE_BODY = "IncompleteBody", 400, "You did not provide the number of bytes specified by the Content-Length HTTP header.";
    CHUNK_SIGNATURE = "SignatureDoesNotMatch", 403, "The chunk signature does not match.";
    NO_SUCH_BUCKET = "NoSuchBucket", 404, "The specified bucket does not exist";
    NO_SUCH_KEY = "NoSuchKey", 404, "The specified key does not exist.";
    NO_SUCH_UPLOAD = "NoSuchUpload", 404, "The specified multipart upload does not exist.";
    BUCKET_OWNED = "BucketAlreadyOwnedByYou", 409, "Your previous request to create the named bucket succeeded and you already own it.";
    BUCKET_NOT_EMPTY = "BucketNotEmpty", 409, "The bucket you tried to delete is not empty";
    INVALID_BUCKET_NAME = "InvalidBucketName", 400, "The specified bucket is not valid.";
    PRECONDITION = "PreconditionFailed", 412, "At least one of the pre-conditions you specified did not hold";
    CONDITIONAL_CONFLICT = "ConditionalRequestConflict", 409, "A conflicting conditional operation is currently in progress against this resource.";
    INVALID_RANGE = "InvalidRange", 416, "The requested range is not satisfiable";
    INVALID_PART_NUMBER = "InvalidPartNumber", 416, "The requested partnumber is not satisfiable";
    INVALID_PART = "InvalidPart", 400, "One or more of the specified parts could not be found.";
    INVALID_PART_ORDER = "InvalidPartOrder", 400, "The list of parts was not in ascending order.";
    ENTITY_TOO_SMALL = "EntityTooSmall", 400, "Your proposed upload is smaller than the minimum allowed object size.";
    ENTITY_TOO_LARGE = "EntityTooLarge", 400, "Your proposed upload exceeds the maximum allowed object size.";
    MALFORMED_XML = "MalformedXML", 400, "The XML you provided was not well-formed or did not validate against our published schema.";
    INVALID_ARGUMENT = "InvalidArgument", 400, "Invalid argument.";
    INVALID_PART_ARG = "InvalidArgument", 400, "Part number must be an integer between 1 and 10000, inclusive";
    INVALID_TOKEN = "InvalidArgument", 400, "The continuation token provided is incorrect";
    NOT_IMPLEMENTED = "NotImplemented", 501, "A header or parameter you provided implies functionality that is not implemented.";
    METHOD_NOT_ALLOWED = "MethodNotAllowed", 405, "The specified method is not allowed against this resource.";
    MISSING_LENGTH = "MissingContentLength", 411, "You must provide the Content-Length HTTP header.";
    INTERNAL = "InternalError", 500, "We encountered an internal error. Please try again.";
    KEY_TOO_LONG = "KeyTooLongError", 400, "Your key is too long.";
    INVALID_COPY_SOURCE = "InvalidArgument", 400, "Copy Source must mention the source bucket and key: sourcebucket/sourcekey.";
    COPY_TO_ITSELF = "InvalidRequest", 400, "This copy request is illegal because it is trying to copy an object to itself without changing the object's metadata.";
    METADATA_TOO_LARGE = "MetadataTooLarge", 400, "Your metadata headers exceed the maximum allowed metadata size.";
    FULL_OBJECT_SHA = "InvalidRequest", 400, "The FULL_OBJECT checksum type is not supported for SHA algorithms.";
    NO_SUCH_BUCKET_POLICY = "NoSuchBucketPolicy", 404, "The bucket policy does not exist";
    NO_SUCH_LIFECYCLE = "NoSuchLifecycleConfiguration", 404, "The lifecycle configuration does not exist";
    NO_SUCH_CORS = "NoSuchCORSConfiguration", 404, "The CORS configuration does not exist";
    NO_SUCH_TAG_SET = "NoSuchTagSet", 404, "The TagSet does not exist";
}

impl From<crate::engine::Error> for ApiError {
    fn from(err: crate::engine::Error) -> Self {
        use crate::engine::Error as E;
        match err {
            E::NoSuchBucket => NO_SUCH_BUCKET.clone(),
            E::BucketExists => BUCKET_OWNED.clone(),
            E::BucketNotEmpty => BUCKET_NOT_EMPTY.clone(),
            E::NoSuchKey => NO_SUCH_KEY.clone(),
            E::NoSuchUpload => NO_SUCH_UPLOAD.clone(),
            E::PreconditionFailed => PRECONDITION.clone(),
            E::InvalidPart => INVALID_PART.clone(),
            E::InvalidPartOrder => INVALID_PART_ORDER.clone(),
            E::EntityTooSmall => ENTITY_TOO_SMALL.clone(),
            E::Conflict => CONDITIONAL_CONFLICT.clone(),
            E::Io(e) => {
                tracing::error!(error = %e, "storage i/o");
                INTERNAL.clone()
            }
            E::Index(e) => {
                tracing::error!(error = %e, "index");
                INTERNAL.clone()
            }
        }
    }
}
