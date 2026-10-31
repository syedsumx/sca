package s3

import (
	"encoding/xml"
	"net/http"
)

type apiError struct {
	Code    string
	Message string
	Status  int
}

func (e *apiError) Error() string { return e.Code + ": " + e.Message }

func newErr(code, msg string, status int) *apiError { return &apiError{code, msg, status} }

var (
	errAccessDenied         = newErr("AccessDenied", "Access Denied", http.StatusForbidden)
	errAuthHeaderMalformed  = newErr("AuthorizationHeaderMalformed", "The authorization header is malformed", http.StatusBadRequest)
	errAuthQueryMalformed   = newErr("AuthorizationQueryParametersError", "The presigned URL parameters are malformed", http.StatusBadRequest)
	errInvalidAccessKey     = newErr("InvalidAccessKeyId", "The access key ID you provided does not exist in our records.", http.StatusForbidden)
	errSignatureMismatch    = newErr("SignatureDoesNotMatch", "The request signature we calculated does not match the signature you provided.", http.StatusForbidden)
	errExpired              = newErr("AccessDenied", "Request has expired", http.StatusForbidden)
	errTimeSkewed           = newErr("RequestTimeTooSkewed", "The difference between the request time and the server's time is too large.", http.StatusForbidden)
	errMissingContentSHA    = newErr("InvalidRequest", "Missing required header for this request: x-amz-content-sha256", http.StatusBadRequest)
	errContentSHAInvalid    = newErr("InvalidArgument", "x-amz-content-sha256 must be UNSIGNED-PAYLOAD, a streaming mode, or a valid sha256 value.", http.StatusBadRequest)
	errMissingDecodedLength = newErr("MissingContentLength", "x-amz-decoded-content-length is required for aws-chunked uploads", http.StatusLengthRequired)
	errInvalidDigestErr     = newErr("InvalidDigest", "The Content-MD5 you specified is not valid.", http.StatusBadRequest)
	errBadDigestErr         = newErr("BadDigest", "The Content-MD5 you specified did not match what we received.", http.StatusBadRequest)
	errContentSHAMismatch   = newErr("XAmzContentSHA256Mismatch", "The provided 'x-amz-content-sha256' header does not match what was computed.", http.StatusBadRequest)
	errChecksumMismatchErr  = newErr("BadDigest", "The checksum you specified did not match the data received.", http.StatusBadRequest)
	errIncompleteBodyErr    = newErr("IncompleteBody", "You did not provide the number of bytes specified by the Content-Length HTTP header.", http.StatusBadRequest)
	errChunkSigErr          = newErr("SignatureDoesNotMatch", "The chunk signature does not match.", http.StatusForbidden)
	errNoSuchBucket         = newErr("NoSuchBucket", "The specified bucket does not exist", http.StatusNotFound)
	errNoSuchKey            = newErr("NoSuchKey", "The specified key does not exist.", http.StatusNotFound)
	errNoSuchUpload         = newErr("NoSuchUpload", "The specified multipart upload does not exist.", http.StatusNotFound)
	errBucketOwned          = newErr("BucketAlreadyOwnedByYou", "Your previous request to create the named bucket succeeded and you already own it.", http.StatusConflict)
	errBucketNotEmpty       = newErr("BucketNotEmpty", "The bucket you tried to delete is not empty", http.StatusConflict)
	errInvalidBucketName    = newErr("InvalidBucketName", "The specified bucket is not valid.", http.StatusBadRequest)
	errPrecondition         = newErr("PreconditionFailed", "At least one of the pre-conditions you specified did not hold", http.StatusPreconditionFailed)
	errConditionalConflict  = newErr("ConditionalRequestConflict", "A conflicting conditional operation is currently in progress against this resource.", http.StatusConflict)
	errInvalidRange         = newErr("InvalidRange", "The requested range is not satisfiable", http.StatusRequestedRangeNotSatisfiable)
	errInvalidPart          = newErr("InvalidPart", "One or more of the specified parts could not be found.", http.StatusBadRequest)
	errInvalidPartOrder     = newErr("InvalidPartOrder", "The list of parts was not in ascending order.", http.StatusBadRequest)
	errEntityTooSmall       = newErr("EntityTooSmall", "Your proposed upload is smaller than the minimum allowed object size.", http.StatusBadRequest)
	errEntityTooLarge       = newErr("EntityTooLarge", "Your proposed upload exceeds the maximum allowed object size.", http.StatusBadRequest)
	errMalformedXML         = newErr("MalformedXML", "The XML you provided was not well-formed or did not validate against our published schema.", http.StatusBadRequest)
	errInvalidArgument      = newErr("InvalidArgument", "Invalid argument.", http.StatusBadRequest)
	errNotImplemented       = newErr("NotImplemented", "A header or parameter you provided implies functionality that is not implemented.", http.StatusNotImplemented)
	errMethodNotAllowed     = newErr("MethodNotAllowed", "The specified method is not allowed against this resource.", http.StatusMethodNotAllowed)
	errMissingLength        = newErr("MissingContentLength", "You must provide the Content-Length HTTP header.", http.StatusLengthRequired)
	errInternal             = newErr("InternalError", "We encountered an internal error. Please try again.", http.StatusInternalServerError)
	errKeyTooLong           = newErr("KeyTooLongError", "Your key is too long.", http.StatusBadRequest)
	errInvalidCopySource    = newErr("InvalidArgument", "Copy Source must mention the source bucket and key: sourcebucket/sourcekey.", http.StatusBadRequest)
	errCopyToItself         = newErr("InvalidRequest", "This copy request is illegal because it is trying to copy an object to itself without changing the object's metadata.", http.StatusBadRequest)
	errNoSuchBucketPolicy   = newErr("NoSuchBucketPolicy", "The bucket policy does not exist", http.StatusNotFound)
	errNoSuchLifecycle      = newErr("NoSuchLifecycleConfiguration", "The lifecycle configuration does not exist", http.StatusNotFound)
	errNoSuchCORS           = newErr("NoSuchCORSConfiguration", "The CORS configuration does not exist", http.StatusNotFound)
	errNoSuchTagSet         = newErr("NoSuchTagSet", "The TagSet does not exist", http.StatusNotFound)
)

type errorBody struct {
	XMLName   xml.Name `xml:"Error"`
	Code      string   `xml:"Code"`
	Message   string   `xml:"Message"`
	Resource  string   `xml:"Resource,omitempty"`
	RequestID string   `xml:"RequestId"`
}
