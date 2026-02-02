"""Infrastructure-as-Code Scanning API routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.iac import IaCScanner

router = APIRouter()


class IaCScanRequest(BaseModel):
    project_path: str
    platforms: list[str] | None = None


class IaCFileRequest(BaseModel):
    file_path: str


@router.post("/iac/scan")
async def scan_iac(request: IaCScanRequest):
    """Scan a project for IaC misconfigurations across all platforms."""
    scan_path = Path(request.project_path)
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    scanner = IaCScanner()
    result = scanner.scan(scan_path)

    # Filter by platform if specified
    if request.platforms:
        allowed = {p.upper() for p in request.platforms}
        result.findings = [f for f in result.findings if f.platform.value in allowed]

    return result.to_dict()


@router.post("/iac/scan-terraform")
async def scan_terraform(request: IaCFileRequest):
    """Scan Terraform files in a directory."""
    scan_path = Path(request.file_path)
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    scanner = IaCScanner()
    findings = scanner.scan_terraform(scan_path)
    return {
        "platform": "TERRAFORM",
        "path": str(scan_path),
        "findings": [f.to_dict() for f in findings],
        "total": len(findings),
    }


@router.post("/iac/scan-cloudformation")
async def scan_cloudformation(request: IaCFileRequest):
    """Scan CloudFormation templates in a directory."""
    scan_path = Path(request.file_path)
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    scanner = IaCScanner()
    findings = scanner.scan_cloudformation(scan_path)
    return {
        "platform": "CLOUDFORMATION",
        "path": str(scan_path),
        "findings": [f.to_dict() for f in findings],
        "total": len(findings),
    }


@router.post("/iac/scan-kubernetes")
async def scan_kubernetes(request: IaCFileRequest):
    """Scan Kubernetes manifests in a directory."""
    scan_path = Path(request.file_path)
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    scanner = IaCScanner()
    findings = scanner.scan_kubernetes(scan_path)
    return {
        "platform": "KUBERNETES",
        "path": str(scan_path),
        "findings": [f.to_dict() for f in findings],
        "total": len(findings),
    }


@router.post("/iac/scan-helm")
async def scan_helm(request: IaCFileRequest):
    """Scan Helm charts in a directory."""
    scan_path = Path(request.file_path)
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    scanner = IaCScanner()
    findings = scanner.scan_helm(scan_path)
    return {
        "platform": "HELM",
        "path": str(scan_path),
        "findings": [f.to_dict() for f in findings],
        "total": len(findings),
    }


@router.get("/iac/rules")
async def list_iac_rules():
    """List all IaC scanning rules."""
    rules = [
        # Terraform
        {"id": "TF0001", "severity": "CRITICAL", "title": "S3 bucket without encryption", "platform": "TERRAFORM"},
        {"id": "TF0002", "severity": "CRITICAL", "title": "S3 bucket public access", "platform": "TERRAFORM"},
        {"id": "TF0003", "severity": "HIGH", "title": "Security group open to world", "platform": "TERRAFORM"},
        {"id": "TF0004", "severity": "HIGH", "title": "RDS without encryption", "platform": "TERRAFORM"},
        {"id": "TF0005", "severity": "CRITICAL", "title": "RDS publicly accessible", "platform": "TERRAFORM"},
        {"id": "TF0006", "severity": "HIGH", "title": "IAM wildcard policy", "platform": "TERRAFORM"},
        {"id": "TF0007", "severity": "MEDIUM", "title": "Missing logging/monitoring", "platform": "TERRAFORM"},
        {"id": "TF0008", "severity": "HIGH", "title": "Unencrypted EBS volume", "platform": "TERRAFORM"},
        {"id": "TF0009", "severity": "MEDIUM", "title": "Default VPC used", "platform": "TERRAFORM"},
        {"id": "TF0010", "severity": "CRITICAL", "title": "Hardcoded secrets", "platform": "TERRAFORM"},
        # CloudFormation
        {"id": "CF0001", "severity": "CRITICAL", "title": "S3 without encryption", "platform": "CLOUDFORMATION"},
        {"id": "CF0002", "severity": "HIGH", "title": "Open security group ingress", "platform": "CLOUDFORMATION"},
        {"id": "CF0003", "severity": "HIGH", "title": "IAM wildcard actions", "platform": "CLOUDFORMATION"},
        {"id": "CF0004", "severity": "HIGH", "title": "RDS without encryption", "platform": "CLOUDFORMATION"},
        {"id": "CF0005", "severity": "CRITICAL", "title": "Secrets in parameters", "platform": "CLOUDFORMATION"},
        {"id": "CF0006", "severity": "MEDIUM", "title": "Lambda without VPC", "platform": "CLOUDFORMATION"},
        {"id": "CF0007", "severity": "MEDIUM", "title": "CloudFront without HTTPS", "platform": "CLOUDFORMATION"},
        {"id": "CF0008", "severity": "LOW", "title": "ELB without access logs", "platform": "CLOUDFORMATION"},
        # Kubernetes
        {"id": "K80001", "severity": "HIGH", "title": "Running as root", "platform": "KUBERNETES"},
        {"id": "K80002", "severity": "CRITICAL", "title": "Privileged container", "platform": "KUBERNETES"},
        {"id": "K80003", "severity": "MEDIUM", "title": "No resource limits", "platform": "KUBERNETES"},
        {"id": "K80004", "severity": "MEDIUM", "title": "Using latest tag", "platform": "KUBERNETES"},
        {"id": "K80005", "severity": "HIGH", "title": "Host network", "platform": "KUBERNETES"},
        {"id": "K80006", "severity": "HIGH", "title": "Host PID", "platform": "KUBERNETES"},
        {"id": "K80007", "severity": "LOW", "title": "Writable root filesystem", "platform": "KUBERNETES"},
        {"id": "K80008", "severity": "CRITICAL", "title": "All capabilities", "platform": "KUBERNETES"},
        {"id": "K80009", "severity": "LOW", "title": "No liveness/readiness probes", "platform": "KUBERNETES"},
        {"id": "K80010", "severity": "INFO", "title": "Default namespace", "platform": "KUBERNETES"},
        {"id": "K80011", "severity": "CRITICAL", "title": "Secrets in env", "platform": "KUBERNETES"},
        {"id": "K80012", "severity": "MEDIUM", "title": "Allow privilege escalation", "platform": "KUBERNETES"},
        # Helm
        {"id": "HM0001", "severity": "CRITICAL", "title": "Hardcoded secrets in values", "platform": "HELM"},
        {"id": "HM0002", "severity": "MEDIUM", "title": "No resource limits", "platform": "HELM"},
        {"id": "HM0003", "severity": "MEDIUM", "title": "Image tag set to latest", "platform": "HELM"},
        {"id": "HM0004", "severity": "MEDIUM", "title": "No securityContext", "platform": "HELM"},
        # Azure Terraform
        {"id": "AZ0001", "severity": "HIGH", "title": "Azure Storage without HTTPS", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0002", "severity": "HIGH", "title": "Azure Storage no infrastructure encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0003", "severity": "HIGH", "title": "NSG rule open to world", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0004", "severity": "MEDIUM", "title": "Azure SQL without auditing", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0005", "severity": "MEDIUM", "title": "Key Vault without purge protection", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0006", "severity": "HIGH", "title": "Managed Disk without encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0007", "severity": "HIGH", "title": "App Service allows HTTP", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0008", "severity": "CRITICAL", "title": "Azure SQL publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0009", "severity": "HIGH", "title": "AKS RBAC disabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0010", "severity": "CRITICAL", "title": "Hardcoded secret in Azure resource", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0011", "severity": "MEDIUM", "title": "Cosmos DB without customer-managed key", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0012", "severity": "CRITICAL", "title": "Cosmos DB publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0013", "severity": "HIGH", "title": "Azure Function allows HTTP", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0014", "severity": "HIGH", "title": "Container Registry admin enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0015", "severity": "MEDIUM", "title": "Container Registry without CMK", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0016", "severity": "MEDIUM", "title": "Service Bus without CMK encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0017", "severity": "MEDIUM", "title": "Event Hub without CMK encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0018", "severity": "MEDIUM", "title": "Log Analytics insufficient retention", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0019", "severity": "HIGH", "title": "Redis Cache non-SSL port enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0020", "severity": "HIGH", "title": "Redis Cache outdated TLS version", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0021", "severity": "HIGH", "title": "Application Gateway without WAF", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0022", "severity": "HIGH", "title": "PostgreSQL without SSL enforcement", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0023", "severity": "CRITICAL", "title": "PostgreSQL publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0024", "severity": "HIGH", "title": "MySQL without SSL enforcement", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0025", "severity": "CRITICAL", "title": "MySQL publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0026", "severity": "MEDIUM", "title": "Azure resource without diagnostic settings", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0027", "severity": "HIGH", "title": "Front Door without WAF policy", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0028", "severity": "HIGH", "title": "Container Instance publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0029", "severity": "HIGH", "title": "Logic App allows HTTP", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0030", "severity": "CRITICAL", "title": "Synapse Workspace publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0031", "severity": "HIGH", "title": "Data Factory publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0032", "severity": "HIGH", "title": "Azure Cognitive Search publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0033", "severity": "HIGH", "title": "Cognitive Services publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0034", "severity": "HIGH", "title": "MariaDB without SSL enforcement", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0035", "severity": "CRITICAL", "title": "MariaDB publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0036", "severity": "HIGH", "title": "Batch Account publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0037", "severity": "MEDIUM", "title": "API Management without VNet integration", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0038", "severity": "HIGH", "title": "Storage Account without network rules", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0039", "severity": "HIGH", "title": "Key Vault without network ACLs", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0040", "severity": "HIGH", "title": "SignalR/Web PubSub publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0041", "severity": "HIGH", "title": "IoT Hub publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0042", "severity": "HIGH", "title": "Web App using outdated TLS version", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0043", "severity": "MEDIUM", "title": "AKS without network policy", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0044", "severity": "INFO", "title": "Public DNS zone without private complement", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0045", "severity": "HIGH", "title": "Databricks Workspace publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0046", "severity": "HIGH", "title": "Purview Account publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0047", "severity": "HIGH", "title": "Container App insecure transport", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0048", "severity": "HIGH", "title": "Spring Cloud App publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0049", "severity": "HIGH", "title": "Automation Account publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0050", "severity": "MEDIUM", "title": "Virtual Network without DDoS protection", "platform": "TERRAFORM_AZURE"},
        # GCP Terraform
        {"id": "GC0002", "severity": "CRITICAL", "title": "GCS bucket publicly accessible", "platform": "TERRAFORM_GCP"},
        {"id": "GC0003", "severity": "HIGH", "title": "GCP firewall open to world", "platform": "TERRAFORM_GCP"},
        {"id": "GC0004", "severity": "HIGH", "title": "Cloud SQL without SSL", "platform": "TERRAFORM_GCP"},
        {"id": "GC0005", "severity": "CRITICAL", "title": "Cloud SQL publicly accessible", "platform": "TERRAFORM_GCP"},
        {"id": "GC0006", "severity": "MEDIUM", "title": "Compute using default service account", "platform": "TERRAFORM_GCP"},
        {"id": "GC0007", "severity": "HIGH", "title": "GKE legacy ABAC enabled", "platform": "TERRAFORM_GCP"},
        {"id": "GC0008", "severity": "MEDIUM", "title": "GKE dashboard enabled", "platform": "TERRAFORM_GCP"},
        {"id": "GC0009", "severity": "HIGH", "title": "Overly permissive IAM role", "platform": "TERRAFORM_GCP"},
        {"id": "GC0010", "severity": "MEDIUM", "title": "KMS key without rotation", "platform": "TERRAFORM_GCP"},
        # OCI Terraform
        {"id": "OC0001", "severity": "CRITICAL", "title": "OCI bucket publicly accessible", "platform": "TERRAFORM_OCI"},
        {"id": "OC0002", "severity": "HIGH", "title": "OCI Security List open to world", "platform": "TERRAFORM_OCI"},
        {"id": "OC0003", "severity": "MEDIUM", "title": "OCI DB without customer-managed key", "platform": "TERRAFORM_OCI"},
        {"id": "OC0004", "severity": "HIGH", "title": "OCI NSG rule open to world", "platform": "TERRAFORM_OCI"},
        {"id": "OC0005", "severity": "MEDIUM", "title": "OCI Boot Volume without CMEK", "platform": "TERRAFORM_OCI"},
        # ARM Templates
        {"id": "ARM0001", "severity": "HIGH", "title": "Storage account allows HTTP", "platform": "ARM"},
        {"id": "ARM0002", "severity": "HIGH", "title": "NSG allows inbound from any", "platform": "ARM"},
        {"id": "ARM0003", "severity": "INFO", "title": "SQL Server audit not inline", "platform": "ARM"},
        {"id": "ARM0004", "severity": "HIGH", "title": "SQL Database TDE disabled", "platform": "ARM"},
        {"id": "ARM0005", "severity": "MEDIUM", "title": "Key Vault without purge protection", "platform": "ARM"},
        {"id": "ARM0006", "severity": "HIGH", "title": "App Service allows HTTP", "platform": "ARM"},
        {"id": "ARM0007", "severity": "MEDIUM", "title": "Disk without explicit encryption", "platform": "ARM"},
        {"id": "ARM0008", "severity": "CRITICAL", "title": "SQL Server publicly accessible", "platform": "ARM"},
        {"id": "ARM0009", "severity": "CRITICAL", "title": "Secret param with default value", "platform": "ARM"},
        # Bicep
        {"id": "BIC0001", "severity": "HIGH", "title": "Storage allows HTTP", "platform": "BICEP"},
        {"id": "BIC0002", "severity": "HIGH", "title": "NSG open to world", "platform": "BICEP"},
        {"id": "BIC0003", "severity": "MEDIUM", "title": "Key Vault without purge protection", "platform": "BICEP"},
        {"id": "BIC0004", "severity": "HIGH", "title": "App Service allows HTTP", "platform": "BICEP"},
        {"id": "BIC0005", "severity": "CRITICAL", "title": "SQL Server publicly accessible", "platform": "BICEP"},
        {"id": "BIC0006", "severity": "HIGH", "title": "AKS RBAC disabled", "platform": "BICEP"},
        {"id": "BIC0007", "severity": "MEDIUM", "title": "Disk without explicit encryption", "platform": "BICEP"},
        {"id": "BIC0008", "severity": "CRITICAL", "title": "Hardcoded secret in Bicep", "platform": "BICEP"},
        {"id": "BIC0009", "severity": "CRITICAL", "title": "Secret param without @secure()", "platform": "BICEP"},
    ]
    return {"rules": rules, "total": len(rules)}
