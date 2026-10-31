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
    INVALID_TAG = "InvalidTag", 400, "The tag provided was not a valid tag.";
    DUPLICATE_TAG = "InvalidTag", 400, "Cannot provide multiple Tags with the same key";
    TOO_MANY_OBJECT_TAGS = "BadRequest", 400, "Object tags cannot be greater than 10";
    TOO_MANY_BUCKET_TAGS = "BadRequest", 400, "Bucket tags cannot be greater than 50";
    ACL_NOT_SUPPORTED = "AccessControlListNotSupported", 400, "The bucket does not allow ACLs";
    CORS_INVALID_METHOD = "InvalidRequest", 400, "Found unsupported HTTP method in CORS config.";
    CORS_INVALID_WILDCARD = "InvalidRequest", 400, "An AllowedOrigin or AllowedHeader can have at most one * wildcard character.";
    CORS_BAD_PREFLIGHT = "BadRequest", 400, "Insufficient information. Origin request header needed.";
    CORS_NOT_ENABLED = "AccessForbidden", 403, "CORSResponse: CORS is not enabled for this bucket.";
    CORS_FORBIDDEN = "AccessForbidden", 403, "CORSResponse: This CORS request is not allowed. This is usually because the evalution of Origin, request method / Access-Control-Request-Method or Access-Control-Request-Headers are not whitelisted by the resource's CORS spec.";
    MALFORMED_POLICY = "MalformedPolicy", 400, "Policies must be valid JSON in the IAM policy language.";
    MALFORMED_POLICY_PRINCIPAL = "MalformedPolicy", 400, "Missing required field Principal";
    MALFORMED_POLICY_RESOURCE = "MalformedPolicy", 400, "Policy has invalid resource";
    MALFORMED_POLICY_ACTION = "MalformedPolicy", 400, "Policy has invalid action";
    MALFORMED_POLICY_CONDITION = "MalformedPolicy", 400, "Policy has an invalid condition operator or key";
    PUBLIC_POLICY_BLOCKED = "AccessDenied", 403, "Public policies are blocked by the public access block for this bucket or store.";
    LIFECYCLE_TRANSITIONS_UNSUPPORTED = "NotImplemented", 501, "Lifecycle transitions to other storage classes are not supported yet.";
    LIFECYCLE_INVALID_DAYS = "InvalidArgument", 400, "Days, NoncurrentDays and DaysAfterInitiation must be positive integers.";
    LIFECYCLE_INVALID_DATE = "InvalidArgument", 400, "Date must be in ISO 8601 format and at midnight UTC.";
    LIFECYCLE_NO_ACTION = "InvalidRequest", 400, "At least one action needs to be specified in a rule.";
    OBJECT_LOCKED = "AccessDenied", 403, "Access Denied because object protected by object lock.";
    LOCK_NOT_CONFIGURED = "InvalidRequest", 400, "Bucket is missing Object Lock Configuration";
    LOCK_HEADERS = "InvalidArgument", 400, "x-amz-object-lock-mode and x-amz-object-lock-retain-until-date must both be supplied, with mode GOVERNANCE or COMPLIANCE.";
    LOCK_INVALID_DATE = "InvalidArgument", 400, "The retain until date must be an ISO 8601 date and time.";
    LOCK_PAST_DATE = "InvalidArgument", 400, "The retain until date must be in the future!";
    LOCK_INVALID_PERIOD = "InvalidArgument", 400, "Default retention period must be a positive integer value.";
    LOCK_NEEDS_CHECKSUM = "InvalidRequest", 400, "Content-MD5 or a checksum header is required for Put Object requests with Object Lock parameters";
    LOCK_COMPLIANCE = "AccessDenied", 403, "Access Denied: COMPLIANCE mode retention cannot be shortened, removed or changed to GOVERNANCE.";
    LOCK_GOVERNANCE = "AccessDenied", 403, "Access Denied: shortening or removing GOVERNANCE retention needs x-amz-bypass-governance-retention and s3:BypassGovernanceRetention.";
    LOCK_NEEDS_VERSIONING_ENABLED = "InvalidBucketState", 409, "Versioning must be 'Enabled' on the bucket to apply a Object Lock configuration";
    NO_SUCH_LOCK_CONFIG = "ObjectLockConfigurationNotFoundError", 404, "Object Lock configuration does not exist for this bucket";
    NO_SUCH_OBJECT_LOCK = "NoSuchObjectLockConfiguration", 404, "The specified object does not have a ObjectLock configuration";
    SSE_NOT_CONFIGURED = "InvalidRequest", 400, "Server-side encryption is not configured on this store (set CAIRN_STORE_KEYRING_FILE or CAIRN_STORE_MASTER_KEY).";
    SSE_INVALID = "InvalidArgument", 400, "The encryption method specified is not supported";
    SSE_CONFLICT = "InvalidArgument", 400, "Server Side Encryption with Customer provided key is incompatible with the encryption method specified";
    SSE_DSSE_UNSUPPORTED = "NotImplemented", 501, "Dual-layer server-side encryption (aws:kms:dsse) is not supported.";
    SSE_C_NEEDS_TLS = "InvalidRequest", 400, "Requests specifying Server Side Encryption with Customer provided keys must be made over a secure connection.";
    SSE_C_HEADERS = "InvalidArgument", 400, "Requests specifying Server Side Encryption with Customer provided keys must provide a valid encryption algorithm (AES256) and a 256-bit key.";
    SSE_C_KEY_MD5 = "InvalidArgument", 400, "The calculated MD5 hash of the key did not match the hash that was provided.";
    SSE_C_REQUIRED = "InvalidRequest", 400, "The object was stored using a form of Server Side Encryption. The correct parameters must be provided to retrieve the object.";
    SSE_C_WRONG_KEY = "AccessDenied", 403, "The provided encryption key does not match the key the object was stored with.";
    KMS_KEY_REQUIRED = "InvalidArgument", 400, "aws:kms encryption needs a key id (x-amz-server-side-encryption-aws-kms-key-id or the bucket's default).";
    KMS_NOT_FOUND = "KMS.NotFoundException", 400, "The key id is not in the store's keyring.";
    KMS_KEY_UNAVAILABLE = "KMS.DisabledException", 500, "The key that encrypted this object is not available in the store's keyring.";
    NO_SUCH_ENCRYPTION = "ServerSideEncryptionConfigurationNotFoundError", 404, "The server side encryption configuration was not found";
    MALFORMED_POST = "MalformedPOSTRequest", 400, "The body of your POST request is not well-formed multipart/form-data.";
    POST_POLICY = "InvalidPolicyDocument", 400, "The policy document is missing, malformed or not signed.";
    POST_EXPIRED = "AccessDenied", 403, "Invalid according to Policy: Policy expired.";
    POST_CONDITION = "AccessDenied", 403, "Invalid according to Policy: Policy Condition failed (or a form field is not covered by a condition).";
    POST_NO_FILE = "InvalidArgument", 400, "POST requires exactly one file upload per request.";
    POST_NO_KEY = "InvalidArgument", 400, "Bucket POST must contain a field named 'key'.";
    QUOTA_EXCEEDED = "QuotaExceeded", 403, "The bucket's quota does not allow this write.";
    MALFORMED_JSON = "MalformedJSON", 400, "The request body must be a JSON document of the documented shape.";
    ADMIN_NOT_FOUND = "NotFound", 404, "No such admin API endpoint.";
    NO_SUCH_WEBSITE = "NoSuchWebsiteConfiguration", 404, "The specified bucket does not have a website configuration";
    WEBSITE_INDEX = "InvalidArgument", 400, "The IndexDocument Suffix is required and may not contain a slash.";
    INVALID_REDIRECT = "InvalidRedirectLocation", 400, "The website redirect location must be non-empty and start with http://, https:// or /.";
    REQUESTER_PAYS_UNSUPPORTED = "NotImplemented", 501, "Requester Pays is not supported: the bucket owner pays.";
    ACCELERATE_UNSUPPORTED = "NotImplemented", 501, "Transfer Acceleration is not supported.";
    ATTRIBUTES_REQUIRED = "InvalidArgument", 400, "x-amz-object-attributes must list one or more of ETag, Checksum, ObjectParts, StorageClass, ObjectSize.";
    EVENTBRIDGE_UNSUPPORTED = "NotImplemented", 501, "EventBridge notifications are not supported; use a Cairn Store event target.";
    EVENT_TARGET_UNKNOWN = "InvalidArgument", 400, "Unable to validate the following destination configurations: the target is not one of this store's event targets.";
    EVENT_NAME_UNSUPPORTED = "InvalidArgument", 400, "The notification configuration names an event type this store does not emit.";
    NO_SUCH_VERSION = "NoSuchVersion", 404, "The specified version does not exist.";
    INVALID_REQUEST_DELETE_MARKER = "InvalidRequest", 400, "The source of a copy request may not specifically refer to a delete marker by version id.";
    LOCK_NEEDS_VERSIONING = "InvalidBucketState", 409, "An Object Lock configuration is present on this bucket, so the versioning state cannot be changed.";
    MFA_DELETE_UNSUPPORTED = "NotImplemented", 501, "MFA Delete is not supported.";
    NO_SUCH_PUBLIC_ACCESS_BLOCK = "NoSuchPublicAccessBlockConfiguration", 404, "The public access block configuration was not found";
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
            E::NoSuchVersion => NO_SUCH_VERSION.clone(),
            E::QuotaExceeded => QUOTA_EXCEEDED.clone(),
            E::ObjectLocked => OBJECT_LOCKED.clone(),
            E::MethodNotAllowed => METHOD_NOT_ALLOWED.clone(),
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
