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
        {"id": "AZ0051", "severity": "HIGH", "title": "Azure Firewall threat intelligence disabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0052", "severity": "MEDIUM", "title": "ExpressRoute without Premium tier", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0053", "severity": "HIGH", "title": "Machine Learning Workspace publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0054", "severity": "HIGH", "title": "Event Grid publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0055", "severity": "MEDIUM", "title": "Stream Analytics without content storage policy", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0056", "severity": "HIGH", "title": "HDInsight cluster without VNet", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0057", "severity": "MEDIUM", "title": "Notification Hub on Free tier", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0058", "severity": "MEDIUM", "title": "Azure resource without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0059", "severity": "MEDIUM", "title": "Azure resource without private endpoint", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0060", "severity": "MEDIUM", "title": "Microsoft Defender for Cloud not enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0061", "severity": "MEDIUM", "title": "VPN Gateway without BGP settings", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0062", "severity": "MEDIUM", "title": "No NAT Gateway for outbound traffic", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0063", "severity": "MEDIUM", "title": "Load Balancer using Basic SKU", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0064", "severity": "MEDIUM", "title": "Traffic Manager using HTTP health checks", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0065", "severity": "HIGH", "title": "Media Services publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0066", "severity": "CRITICAL", "title": "Data Lake Store encryption disabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0067", "severity": "MEDIUM", "title": "No Bastion Host for VM access", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0068", "severity": "INFO", "title": "No Azure Policy assignments detected", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0069", "severity": "MEDIUM", "title": "No backup vault for virtual machines", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0070", "severity": "MEDIUM", "title": "No Azure Monitor action groups or alerts", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0071", "severity": "MEDIUM", "title": "VM without disk encryption set", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0072", "severity": "MEDIUM", "title": "No Network Watcher or NSG flow logs", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0073", "severity": "HIGH", "title": "App Configuration publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0074", "severity": "MEDIUM", "title": "Azure Maps local authentication enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0075", "severity": "HIGH", "title": "CDN endpoint allows HTTP", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0076", "severity": "MEDIUM", "title": "Container App Environment not internal", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0077", "severity": "HIGH", "title": "Service Fabric without Azure AD auth", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0078", "severity": "HIGH", "title": "Digital Twins without private endpoint", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0079", "severity": "CRITICAL", "title": "Azure OpenAI publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0080", "severity": "HIGH", "title": "Managed Grafana publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0081", "severity": "HIGH", "title": "Data Factory publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0082", "severity": "MEDIUM", "title": "Logic App without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0083", "severity": "HIGH", "title": "API Management backend without HTTPS", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0084", "severity": "CRITICAL", "title": "Synapse Workspace publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0085", "severity": "MEDIUM", "title": "Stream Analytics without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0086", "severity": "HIGH", "title": "Purview Account publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0087", "severity": "HIGH", "title": "Batch Account without private network", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0088", "severity": "MEDIUM", "title": "Notification Hub without authentication", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0089", "severity": "HIGH", "title": "SignalR Service publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0090", "severity": "MEDIUM", "title": "Static Web App without authentication", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0091", "severity": "HIGH", "title": "Automation Account publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0092", "severity": "HIGH", "title": "Cognitive Account publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0093", "severity": "MEDIUM", "title": "Communication Service without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0094", "severity": "HIGH", "title": "Container Group publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0095", "severity": "HIGH", "title": "Data Explorer publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0096", "severity": "HIGH", "title": "Databricks publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0097", "severity": "HIGH", "title": "Healthcare FHIR publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0098", "severity": "HIGH", "title": "IoT Hub publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0099", "severity": "HIGH", "title": "Machine Learning publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0100", "severity": "MEDIUM", "title": "Media Services without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0101", "severity": "HIGH", "title": "Managed Disk without encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0102", "severity": "MEDIUM", "title": "Private DNS Zone without VNet link", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0103", "severity": "HIGH", "title": "App Service Slot without HTTPS", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0104", "severity": "HIGH", "title": "Redis Cache without TLS", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0105", "severity": "HIGH", "title": "Search Service publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0106", "severity": "MEDIUM", "title": "Spring Cloud without VNet", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0107", "severity": "HIGH", "title": "Web PubSub publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0108", "severity": "HIGH", "title": "Front Door without WAF", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0109", "severity": "HIGH", "title": "Application Gateway without WAF", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0110", "severity": "MEDIUM", "title": "DNS Zone without DNSSEC", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0111", "severity": "MEDIUM", "title": "ExpressRoute without encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0112", "severity": "HIGH", "title": "Firewall without threat intelligence", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0113", "severity": "MEDIUM", "title": "Image Builder without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0114", "severity": "HIGH", "title": "Key Vault without purge protection", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0115", "severity": "MEDIUM", "title": "Load Balancer without health probe", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0116", "severity": "MEDIUM", "title": "Log Analytics without CMK", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0117", "severity": "MEDIUM", "title": "Monitor diagnostic settings missing", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0118", "severity": "CRITICAL", "title": "MySQL Flexible Server publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0119", "severity": "MEDIUM", "title": "Network Interface with public IP", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0120", "severity": "CRITICAL", "title": "PostgreSQL Flexible Server publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0121", "severity": "HIGH", "title": "Recovery Vault without encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0122", "severity": "LOW", "title": "Route Table without BGP propagation", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0123", "severity": "HIGH", "title": "Service Bus publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0124", "severity": "HIGH", "title": "Snapshot without encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0125", "severity": "MEDIUM", "title": "Storage Sync without private endpoint", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0126", "severity": "MEDIUM", "title": "Virtual Hub without firewall", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0127", "severity": "MEDIUM", "title": "VM Scale Set without health extension", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0128", "severity": "MEDIUM", "title": "VNet without DDoS protection", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0129", "severity": "MEDIUM", "title": "VPN Gateway without active-active", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0130", "severity": "HIGH", "title": "WAF policy in detection-only mode", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0131", "severity": "HIGH", "title": "Event Grid topic publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0132", "severity": "HIGH", "title": "Event Hub namespace publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0133", "severity": "HIGH", "title": "Cosmos DB publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0134", "severity": "HIGH", "title": "Function App publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0135", "severity": "HIGH", "title": "Container Registry publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0136", "severity": "HIGH", "title": "Managed HSM without purge protection", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0137", "severity": "MEDIUM", "title": "Data Protection Vault without immutability", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0138", "severity": "MEDIUM", "title": "Private Endpoint without DNS zone group", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0139", "severity": "HIGH", "title": "Managed Disk public network access", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0140", "severity": "LOW", "title": "Resources without maintenance configuration", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0141", "severity": "MEDIUM", "title": "Firewall Policy without TLS inspection", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0142", "severity": "HIGH", "title": "SQL Server without AAD administrator", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0143", "severity": "MEDIUM", "title": "SQL audit retention too short", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0144", "severity": "MEDIUM", "title": "App Service without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0145", "severity": "HIGH", "title": "App Service remote debugging enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0146", "severity": "HIGH", "title": "App Service FTP access enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0147", "severity": "HIGH", "title": "Storage Account allows blob public access", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0148", "severity": "MEDIUM", "title": "Storage Account shared key access enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0149", "severity": "HIGH", "title": "Key Vault soft delete disabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0150", "severity": "MEDIUM", "title": "Key Vault RBAC not enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0151", "severity": "MEDIUM", "title": "AKS without Azure Policy addon", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0152", "severity": "HIGH", "title": "AKS API server publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0153", "severity": "MEDIUM", "title": "AKS without disk encryption set", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0154", "severity": "LOW", "title": "VM without boot diagnostics", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0155", "severity": "HIGH", "title": "Linux VM password authentication enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0156", "severity": "CRITICAL", "title": "SQL Database TDE disabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0157", "severity": "MEDIUM", "title": "Cosmos DB without automatic failover", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0158", "severity": "HIGH", "title": "Service Bus namespace publicly accessible", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0159", "severity": "LOW", "title": "Event Hub without capture enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0160", "severity": "MEDIUM", "title": "Function App without managed identity", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0161", "severity": "HIGH", "title": "App Service CORS wildcard origin", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0162", "severity": "MEDIUM", "title": "PostgreSQL Flexible Server low backup retention", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0163", "severity": "MEDIUM", "title": "MySQL Flexible Server low backup retention", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0164", "severity": "MEDIUM", "title": "Container Registry without content trust", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0165", "severity": "LOW", "title": "Container Registry without quarantine policy", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0166", "severity": "MEDIUM", "title": "AKS without automatic upgrade", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0167", "severity": "LOW", "title": "Application Insights without workspace", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0168", "severity": "MEDIUM", "title": "Key Vault certificate without auto-rotation", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0169", "severity": "HIGH", "title": "Front Door route without HTTPS redirect", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0170", "severity": "HIGH", "title": "CDN custom domain without HTTPS", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0171", "severity": "MEDIUM", "title": "SQL Server without vulnerability assessment", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0172", "severity": "MEDIUM", "title": "App Service outdated runtime version", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0173", "severity": "MEDIUM", "title": "NSG flow logs not configured", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0174", "severity": "LOW", "title": "Storage Account without lifecycle management", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0175", "severity": "MEDIUM", "title": "AKS without container insights", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0176", "severity": "HIGH", "title": "Cosmos DB without network restriction", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0177", "severity": "MEDIUM", "title": "Application Gateway without SSL policy", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0178", "severity": "LOW", "title": "AKS without node OS upgrade channel", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0179", "severity": "LOW", "title": "Firewall without DNS proxy", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0180", "severity": "HIGH", "title": "SQL Server minimum TLS below 1.2", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0181", "severity": "MEDIUM", "title": "PostgreSQL without threat detection", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0182", "severity": "MEDIUM", "title": "Storage Account without infrastructure encryption", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0183", "severity": "LOW", "title": "App Service client certificates disabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0184", "severity": "HIGH", "title": "Function App allows HTTP", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0185", "severity": "HIGH", "title": "Redis Cache minimum TLS below 1.2", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0186", "severity": "MEDIUM", "title": "Cosmos DB local authentication enabled", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0187", "severity": "MEDIUM", "title": "Container App without ingress restriction", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0188", "severity": "MEDIUM", "title": "App Service without VNet integration", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0189", "severity": "LOW", "title": "SQL Database without long-term retention", "platform": "TERRAFORM_AZURE"},
        {"id": "AZ0190", "severity": "MEDIUM", "title": "Key Vault without diagnostic settings", "platform": "TERRAFORM_AZURE"},
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
