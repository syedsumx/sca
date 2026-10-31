package s3

import "encoding/xml"

const xmlns = "http://s3.amazonaws.com/doc/2006-03-01/"

type owner struct {
	ID          string `xml:"ID"`
	DisplayName string `xml:"DisplayName"`
}

type listAllMyBuckets struct {
	XMLName xml.Name `xml:"ListAllMyBucketsResult"`
	Xmlns   string   `xml:"xmlns,attr"`
	Owner   owner    `xml:"Owner"`
	Buckets []bucket `xml:"Buckets>Bucket"`
}

type bucket struct {
	Name         string `xml:"Name"`
	CreationDate string `xml:"CreationDate"`
}

type contents struct {
	Key          string   `xml:"Key"`
	LastModified string   `xml:"LastModified"`
	ETag         string   `xml:"ETag"`
	Size         int64    `xml:"Size"`
	StorageClass string   `xml:"StorageClass"`
	Owner        *owner   `xml:"Owner,omitempty"`
	ChecksumAlgo []string `xml:"ChecksumAlgorithm,omitempty"`
	ChecksumType string   `xml:"ChecksumType,omitempty"`
}

type commonPrefix struct {
	Prefix string `xml:"Prefix"`
}

type listV2 struct {
	XMLName               xml.Name       `xml:"ListBucketResult"`
	Xmlns                 string         `xml:"xmlns,attr"`
	Name                  string         `xml:"Name"`
	Prefix                string         `xml:"Prefix"`
	Delimiter             string         `xml:"Delimiter,omitempty"`
	StartAfter            string         `xml:"StartAfter,omitempty"`
	ContinuationToken     string         `xml:"ContinuationToken,omitempty"`
	NextContinuationToken string         `xml:"NextContinuationToken,omitempty"`
	KeyCount              int            `xml:"KeyCount"`
	MaxKeys               int            `xml:"MaxKeys"`
	EncodingType          string         `xml:"EncodingType,omitempty"`
	IsTruncated           bool           `xml:"IsTruncated"`
	Contents              []contents     `xml:"Contents"`
	CommonPrefixes        []commonPrefix `xml:"CommonPrefixes"`
}

type listV1 struct {
	XMLName        xml.Name       `xml:"ListBucketResult"`
	Xmlns          string         `xml:"xmlns,attr"`
	Name           string         `xml:"Name"`
	Prefix         string         `xml:"Prefix"`
	Marker         string         `xml:"Marker"`
	NextMarker     string         `xml:"NextMarker,omitempty"`
	Delimiter      string         `xml:"Delimiter,omitempty"`
	MaxKeys        int            `xml:"MaxKeys"`
	EncodingType   string         `xml:"EncodingType,omitempty"`
	IsTruncated    bool           `xml:"IsTruncated"`
	Contents       []contents     `xml:"Contents"`
	CommonPrefixes []commonPrefix `xml:"CommonPrefixes"`
}

type initiateResult struct {
	XMLName  xml.Name `xml:"InitiateMultipartUploadResult"`
	Xmlns    string   `xml:"xmlns,attr"`
	Bucket   string   `xml:"Bucket"`
	Key      string   `xml:"Key"`
	UploadID string   `xml:"UploadId"`
}

type completeRequest struct {
	XMLName xml.Name `xml:"CompleteMultipartUpload"`
	Parts   []struct {
		PartNumber        int    `xml:"PartNumber"`
		ETag              string `xml:"ETag"`
		ChecksumCRC32     string `xml:"ChecksumCRC32"`
		ChecksumCRC32C    string `xml:"ChecksumCRC32C"`
		ChecksumCRC64NVME string `xml:"ChecksumCRC64NVME"`
		ChecksumSHA1      string `xml:"ChecksumSHA1"`
		ChecksumSHA256    string `xml:"ChecksumSHA256"`
	} `xml:"Part"`
}

type completeResult struct {
	XMLName           xml.Name `xml:"CompleteMultipartUploadResult"`
	Xmlns             string   `xml:"xmlns,attr"`
	Location          string   `xml:"Location"`
	Bucket            string   `xml:"Bucket"`
	Key               string   `xml:"Key"`
	ETag              string   `xml:"ETag"`
	ChecksumCRC32     string   `xml:"ChecksumCRC32,omitempty"`
	ChecksumCRC32C    string   `xml:"ChecksumCRC32C,omitempty"`
	ChecksumCRC64NVME string   `xml:"ChecksumCRC64NVME,omitempty"`
	ChecksumSHA1      string   `xml:"ChecksumSHA1,omitempty"`
	ChecksumSHA256    string   `xml:"ChecksumSHA256,omitempty"`
	ChecksumType      string   `xml:"ChecksumType,omitempty"`
}

type partXML struct {
	PartNumber        int    `xml:"PartNumber"`
	LastModified      string `xml:"LastModified"`
	ETag              string `xml:"ETag"`
	Size              int64  `xml:"Size"`
	ChecksumCRC32     string `xml:"ChecksumCRC32,omitempty"`
	ChecksumCRC32C    string `xml:"ChecksumCRC32C,omitempty"`
	ChecksumCRC64NVME string `xml:"ChecksumCRC64NVME,omitempty"`
	ChecksumSHA1      string `xml:"ChecksumSHA1,omitempty"`
	ChecksumSHA256    string `xml:"ChecksumSHA256,omitempty"`
}

type listPartsResult struct {
	XMLName              xml.Name  `xml:"ListPartsResult"`
	Xmlns                string    `xml:"xmlns,attr"`
	Bucket               string    `xml:"Bucket"`
	Key                  string    `xml:"Key"`
	UploadID             string    `xml:"UploadId"`
	Initiator            owner     `xml:"Initiator"`
	Owner                owner     `xml:"Owner"`
	StorageClass         string    `xml:"StorageClass"`
	PartNumberMarker     int       `xml:"PartNumberMarker"`
	NextPartNumberMarker int       `xml:"NextPartNumberMarker"`
	MaxParts             int       `xml:"MaxParts"`
	IsTruncated          bool      `xml:"IsTruncated"`
	ChecksumAlgorithm    string    `xml:"ChecksumAlgorithm,omitempty"`
	Parts                []partXML `xml:"Part"`
}

type uploadXML struct {
	Key          string `xml:"Key"`
	UploadID     string `xml:"UploadId"`
	Initiator    owner  `xml:"Initiator"`
	Owner        owner  `xml:"Owner"`
	StorageClass string `xml:"StorageClass"`
	Initiated    string `xml:"Initiated"`
}

type listUploadsResult struct {
	XMLName            xml.Name       `xml:"ListMultipartUploadsResult"`
	Xmlns              string         `xml:"xmlns,attr"`
	Bucket             string         `xml:"Bucket"`
	KeyMarker          string         `xml:"KeyMarker"`
	UploadIDMarker     string         `xml:"UploadIdMarker"`
	NextKeyMarker      string         `xml:"NextKeyMarker"`
	NextUploadIDMarker string         `xml:"NextUploadIdMarker"`
	Prefix             string         `xml:"Prefix"`
	Delimiter          string         `xml:"Delimiter,omitempty"`
	MaxUploads         int            `xml:"MaxUploads"`
	IsTruncated        bool           `xml:"IsTruncated"`
	Uploads            []uploadXML    `xml:"Upload"`
	CommonPrefixes     []commonPrefix `xml:"CommonPrefixes"`
}

type deleteRequest struct {
	XMLName xml.Name `xml:"Delete"`
	Quiet   bool     `xml:"Quiet"`
	Objects []struct {
		Key       string `xml:"Key"`
		VersionID string `xml:"VersionId"`
	} `xml:"Object"`
}

type deletedXML struct {
	Key string `xml:"Key"`
}

type deleteErrXML struct {
	Key     string `xml:"Key"`
	Code    string `xml:"Code"`
	Message string `xml:"Message"`
}

type deleteResult struct {
	XMLName xml.Name       `xml:"DeleteResult"`
	Xmlns   string         `xml:"xmlns,attr"`
	Deleted []deletedXML   `xml:"Deleted"`
	Errors  []deleteErrXML `xml:"Error"`
}

type copyObjectResult struct {
	XMLName      xml.Name `xml:"CopyObjectResult"`
	Xmlns        string   `xml:"xmlns,attr"`
	LastModified string   `xml:"LastModified"`
	ETag         string   `xml:"ETag"`
}

type copyPartResult struct {
	XMLName      xml.Name `xml:"CopyPartResult"`
	Xmlns        string   `xml:"xmlns,attr"`
	LastModified string   `xml:"LastModified"`
	ETag         string   `xml:"ETag"`
}

type locationConstraint struct {
	XMLName xml.Name `xml:"LocationConstraint"`
	Xmlns   string   `xml:"xmlns,attr"`
	Value   string   `xml:",chardata"`
}

type versioningConfiguration struct {
	XMLName xml.Name `xml:"VersioningConfiguration"`
	Xmlns   string   `xml:"xmlns,attr"`
}

type accessControlPolicy struct {
	XMLName xml.Name `xml:"AccessControlPolicy"`
	Xmlns   string   `xml:"xmlns,attr"`
	Owner   owner    `xml:"Owner"`
	Grants  []grant  `xml:"AccessControlList>Grant"`
}

type grant struct {
	Grantee struct {
		XMLNS       string `xml:"xmlns:xsi,attr"`
		Type        string `xml:"xsi:type,attr"`
		ID          string `xml:"ID"`
		DisplayName string `xml:"DisplayName"`
	} `xml:"Grantee"`
	Permission string `xml:"Permission"`
}

type createBucketConfiguration struct {
	XMLName            xml.Name `xml:"CreateBucketConfiguration"`
	LocationConstraint string   `xml:"LocationConstraint"`
}
