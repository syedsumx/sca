# CodeScope Rule Catalog

*Auto-generated on 2026-02-06 16:45:14*

This document contains all rules available in CodeScope, organized by language and category.

## Table of Contents

- [Summary](#summary)
- [Rules by Language](#rules-by-language)
- [Rules by Severity](#rules-by-severity)
- [Security Rules (OWASP/CWE)](#security-rules)
- [All Rules](#all-rules)

## Summary

**Total Rules:** 76

**Total Remediation Effort:** 1654 minutes (27 hours)

### By Severity

| Severity | Count |
|----------|-------|
| ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) | 36 |
| ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) | 7 |
| ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) | 18 |
| ![MINOR](https://img.shields.io/badge/-MINOR-blue) | 14 |
| ![INFO](https://img.shields.io/badge/-INFO-gray) | 1 |

### By Type

| Type | Count |
|------|-------|
| 🐛 BUG | 6 |
| 🔓 VULNERABILITY | 45 |
| 👃 CODE_SMELL | 25 |
| 🔥 SECURITY_HOTSPOT | 0 |

### By Language

| Language | Rules |
|----------|-------|
| csharp | 5 |
| go | 4 |
| java | 6 |
| javascript | 27 |
| php | 5 |
| python | 24 |
| ruby | 5 |
| typescript | 27 |

## Rules by Language

### Csharp

*5 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [csharp:S2068](#csharps2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [csharp:S2076](#csharps2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [csharp:S2755](#csharps2755) | XML External Entity (XXE) | BLOCKER | 🔓 VULNERABILITY |
| [csharp:S3649](#csharps3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [csharp:S5135](#csharps5135) | Unsafe Deserialization | BLOCKER | 🔓 VULNERABILITY |

### Go

*4 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [go:S2068](#gos2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [go:S2076](#gos2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [go:S3649](#gos3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [go:S4830](#gos4830) | Insecure TLS Configuration | BLOCKER | 🔓 VULNERABILITY |

### Java

*6 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [java:S2068](#javas2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [java:S2076](#javas2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [java:S2083](#javas2083) | Path Traversal | BLOCKER | 🔓 VULNERABILITY |
| [java:S2755](#javas2755) | XML External Entity (XXE) | BLOCKER | 🔓 VULNERABILITY |
| [java:S3649](#javas3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [java:S5135](#javas5135) | Unsafe Deserialization | BLOCKER | 🔓 VULNERABILITY |

### Javascript

*27 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [javascript:S107](#javascripts107) | Too Many Parameters | MAJOR | 👃 CODE_SMELL |
| [javascript:S109](#javascripts109) | Magic Number | MINOR | 👃 CODE_SMELL |
| [javascript:S1186](#javascripts1186) | Empty Function | MINOR | 👃 CODE_SMELL |
| [javascript:S1192](#javascripts1192) | Duplicated String | MINOR | 👃 CODE_SMELL |
| [javascript:S134](#javascripts134) | Deep Nesting | MAJOR | 👃 CODE_SMELL |
| [javascript:S138](#javascripts138) | Function Too Long | MAJOR | 👃 CODE_SMELL |
| [javascript:S1440](#javascripts1440) | Use Strict Equality | MINOR | 👃 CODE_SMELL |
| [javascript:S1481](#javascripts1481) | Unused Variable | MINOR | 👃 CODE_SMELL |
| [javascript:S1523](#javascripts1523) | Eval Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2068](#javascripts2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2076](#javascripts2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2083](#javascripts2083) | Path Traversal | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2092](#javascripts2092) | Insecure Cookie | MAJOR | 🔓 VULNERABILITY |
| [javascript:S2228](#javascripts2228) | Console Log | MINOR | 👃 CODE_SMELL |
| [javascript:S2245](#javascripts2245) | Insecure Randomness | CRITICAL | 🔓 VULNERABILITY |
| [javascript:S2486](#javascripts2486) | Empty Catch Block | MAJOR | 👃 CODE_SMELL |
| [javascript:S3504](#javascripts3504) | Avoid var | MINOR | 👃 CODE_SMELL |
| [javascript:S3649](#javascripts3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S4326](#javascripts4326) | Async Without Await | MINOR | 👃 CODE_SMELL |
| [javascript:S5131](#javascripts5131) | Cross-Site Scripting (XSS) | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S5144](#javascripts5144) | SSRF (Server-Side Request Forgery) | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S5146](#javascripts5146) | Open Redirect | MAJOR | 🔓 VULNERABILITY |
| [javascript:S5147](#javascripts5147) | Prototype Pollution | CRITICAL | 🔓 VULNERABILITY |
| [javascript:S5334](#javascripts5334) | NoSQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S5547](#javascripts5547) | Weak Cryptography | CRITICAL | 🔓 VULNERABILITY |
| [javascript:S5765](#javascripts5765) | Callback Hell | MAJOR | 👃 CODE_SMELL |
| [javascript:S5852](#javascripts5852) | ReDoS (Regex DoS) | CRITICAL | 🔓 VULNERABILITY |

### Php

*5 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [php:S2068](#phps2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [php:S2076](#phps2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [php:S2083](#phps2083) | File Inclusion | BLOCKER | 🔓 VULNERABILITY |
| [php:S3649](#phps3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [php:S5131](#phps5131) | Cross-Site Scripting (XSS) | BLOCKER | 🔓 VULNERABILITY |

### Python

*24 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [python:S107](#pythons107) | Too Many Parameters | MAJOR | 👃 CODE_SMELL |
| [python:S108](#pythons108) | Empty Block | MINOR | 👃 CODE_SMELL |
| [python:S1125](#pythons1125) | Comparison to Boolean | MINOR | 👃 CODE_SMELL |
| [python:S1192](#pythons1192) | Duplicate String Literal | MINOR | 👃 CODE_SMELL |
| [python:S134](#pythons134) | Deep Nesting | MAJOR | 👃 CODE_SMELL |
| [python:S138](#pythons138) | Function Too Long | MAJOR | 👃 CODE_SMELL |
| [python:S1481](#pythons1481) | Unused Variable | MINOR | 👃 CODE_SMELL |
| [python:S1541](#pythons1541) | Cyclomatic Complexity | MAJOR | 👃 CODE_SMELL |
| [python:S2068](#pythons2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [python:S2076](#pythons2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [python:S2083](#pythons2083) | Path Traversal | BLOCKER | 🔓 VULNERABILITY |
| [python:S2154](#pythons2154) | Reassign Builtin | MAJOR | 🐛 BUG |
| [python:S2189](#pythons2189) | Print Statement | INFO | 👃 CODE_SMELL |
| [python:S2230](#pythons2230) | God Class | MAJOR | 👃 CODE_SMELL |
| [python:S2245](#pythons2245) | Insecure Random | CRITICAL | 🔓 VULNERABILITY |
| [python:S2711](#pythons2711) | Comparison to None | MINOR | 🐛 BUG |
| [python:S2737](#pythons2737) | Except Pass | MAJOR | 🐛 BUG |
| [python:S2890](#pythons2890) | Global Statement | MINOR | 👃 CODE_SMELL |
| [python:S3649](#pythons3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [python:S3776](#pythons3776) | Cognitive Complexity | MAJOR | 👃 CODE_SMELL |
| [python:S4790](#pythons4790) | Weak Cryptographic Hash | CRITICAL | 🔓 VULNERABILITY |
| [python:S5754](#pythons5754) | Bare Except | MAJOR | 🐛 BUG |
| [python:S5765](#pythons5765) | Return in Finally | MAJOR | 🐛 BUG |
| [python:S5915](#pythons5915) | Assert for Validation | MAJOR | 🐛 BUG |

### Ruby

*5 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [ruby:S2068](#rubys2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [ruby:S2076](#rubys2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [ruby:S3649](#rubys3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [ruby:S5131](#rubys5131) | Cross-Site Scripting (XSS) | BLOCKER | 🔓 VULNERABILITY |
| [ruby:S5334](#rubys5334) | Mass Assignment | CRITICAL | 🔓 VULNERABILITY |

### Typescript

*27 rules*

| Rule ID | Name | Severity | Type |
|---------|------|----------|------|
| [javascript:S107](#javascripts107) | Too Many Parameters | MAJOR | 👃 CODE_SMELL |
| [javascript:S109](#javascripts109) | Magic Number | MINOR | 👃 CODE_SMELL |
| [javascript:S1186](#javascripts1186) | Empty Function | MINOR | 👃 CODE_SMELL |
| [javascript:S1192](#javascripts1192) | Duplicated String | MINOR | 👃 CODE_SMELL |
| [javascript:S134](#javascripts134) | Deep Nesting | MAJOR | 👃 CODE_SMELL |
| [javascript:S138](#javascripts138) | Function Too Long | MAJOR | 👃 CODE_SMELL |
| [javascript:S1440](#javascripts1440) | Use Strict Equality | MINOR | 👃 CODE_SMELL |
| [javascript:S1481](#javascripts1481) | Unused Variable | MINOR | 👃 CODE_SMELL |
| [javascript:S1523](#javascripts1523) | Eval Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2068](#javascripts2068) | Hardcoded Credentials | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2076](#javascripts2076) | Command Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2083](#javascripts2083) | Path Traversal | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S2092](#javascripts2092) | Insecure Cookie | MAJOR | 🔓 VULNERABILITY |
| [javascript:S2228](#javascripts2228) | Console Log | MINOR | 👃 CODE_SMELL |
| [javascript:S2245](#javascripts2245) | Insecure Randomness | CRITICAL | 🔓 VULNERABILITY |
| [javascript:S2486](#javascripts2486) | Empty Catch Block | MAJOR | 👃 CODE_SMELL |
| [javascript:S3504](#javascripts3504) | Avoid var | MINOR | 👃 CODE_SMELL |
| [javascript:S3649](#javascripts3649) | SQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S4326](#javascripts4326) | Async Without Await | MINOR | 👃 CODE_SMELL |
| [javascript:S5131](#javascripts5131) | Cross-Site Scripting (XSS) | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S5144](#javascripts5144) | SSRF (Server-Side Request Forgery) | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S5146](#javascripts5146) | Open Redirect | MAJOR | 🔓 VULNERABILITY |
| [javascript:S5147](#javascripts5147) | Prototype Pollution | CRITICAL | 🔓 VULNERABILITY |
| [javascript:S5334](#javascripts5334) | NoSQL Injection | BLOCKER | 🔓 VULNERABILITY |
| [javascript:S5547](#javascripts5547) | Weak Cryptography | CRITICAL | 🔓 VULNERABILITY |
| [javascript:S5765](#javascripts5765) | Callback Hell | MAJOR | 👃 CODE_SMELL |
| [javascript:S5852](#javascripts5852) | ReDoS (Regex DoS) | CRITICAL | 🔓 VULNERABILITY |

## Rules by Severity

### BLOCKER

*36 rules*

- [csharp:S2068](#csharps2068) - Hardcoded Credentials
- [csharp:S2076](#csharps2076) - Command Injection
- [csharp:S2755](#csharps2755) - XML External Entity (XXE)
- [csharp:S3649](#csharps3649) - SQL Injection
- [csharp:S5135](#csharps5135) - Unsafe Deserialization
- [go:S2068](#gos2068) - Hardcoded Credentials
- [go:S2076](#gos2076) - Command Injection
- [go:S3649](#gos3649) - SQL Injection
- [go:S4830](#gos4830) - Insecure TLS Configuration
- [java:S2068](#javas2068) - Hardcoded Credentials
- [java:S2076](#javas2076) - Command Injection
- [java:S2083](#javas2083) - Path Traversal
- [java:S2755](#javas2755) - XML External Entity (XXE)
- [java:S3649](#javas3649) - SQL Injection
- [java:S5135](#javas5135) - Unsafe Deserialization
- [javascript:S1523](#javascripts1523) - Eval Injection
- [javascript:S2068](#javascripts2068) - Hardcoded Credentials
- [javascript:S2076](#javascripts2076) - Command Injection
- [javascript:S2083](#javascripts2083) - Path Traversal
- [javascript:S3649](#javascripts3649) - SQL Injection
- [javascript:S5131](#javascripts5131) - Cross-Site Scripting (XSS)
- [javascript:S5144](#javascripts5144) - SSRF (Server-Side Request Forgery)
- [javascript:S5334](#javascripts5334) - NoSQL Injection
- [php:S2068](#phps2068) - Hardcoded Credentials
- [php:S2076](#phps2076) - Command Injection
- [php:S2083](#phps2083) - File Inclusion
- [php:S3649](#phps3649) - SQL Injection
- [php:S5131](#phps5131) - Cross-Site Scripting (XSS)
- [python:S2068](#pythons2068) - Hardcoded Credentials
- [python:S2076](#pythons2076) - Command Injection
- [python:S2083](#pythons2083) - Path Traversal
- [python:S3649](#pythons3649) - SQL Injection
- [ruby:S2068](#rubys2068) - Hardcoded Credentials
- [ruby:S2076](#rubys2076) - Command Injection
- [ruby:S3649](#rubys3649) - SQL Injection
- [ruby:S5131](#rubys5131) - Cross-Site Scripting (XSS)

### CRITICAL

*7 rules*

- [javascript:S2245](#javascripts2245) - Insecure Randomness
- [javascript:S5147](#javascripts5147) - Prototype Pollution
- [javascript:S5547](#javascripts5547) - Weak Cryptography
- [javascript:S5852](#javascripts5852) - ReDoS (Regex DoS)
- [python:S2245](#pythons2245) - Insecure Random
- [python:S4790](#pythons4790) - Weak Cryptographic Hash
- [ruby:S5334](#rubys5334) - Mass Assignment

### MAJOR

*18 rules*

- [javascript:S107](#javascripts107) - Too Many Parameters
- [javascript:S134](#javascripts134) - Deep Nesting
- [javascript:S138](#javascripts138) - Function Too Long
- [javascript:S2092](#javascripts2092) - Insecure Cookie
- [javascript:S2486](#javascripts2486) - Empty Catch Block
- [javascript:S5146](#javascripts5146) - Open Redirect
- [javascript:S5765](#javascripts5765) - Callback Hell
- [python:S107](#pythons107) - Too Many Parameters
- [python:S134](#pythons134) - Deep Nesting
- [python:S138](#pythons138) - Function Too Long
- [python:S1541](#pythons1541) - Cyclomatic Complexity
- [python:S2154](#pythons2154) - Reassign Builtin
- [python:S2230](#pythons2230) - God Class
- [python:S2737](#pythons2737) - Except Pass
- [python:S3776](#pythons3776) - Cognitive Complexity
- [python:S5754](#pythons5754) - Bare Except
- [python:S5765](#pythons5765) - Return in Finally
- [python:S5915](#pythons5915) - Assert for Validation

### MINOR

*14 rules*

- [javascript:S109](#javascripts109) - Magic Number
- [javascript:S1186](#javascripts1186) - Empty Function
- [javascript:S1192](#javascripts1192) - Duplicated String
- [javascript:S1440](#javascripts1440) - Use Strict Equality
- [javascript:S1481](#javascripts1481) - Unused Variable
- [javascript:S2228](#javascripts2228) - Console Log
- [javascript:S3504](#javascripts3504) - Avoid var
- [javascript:S4326](#javascripts4326) - Async Without Await
- [python:S108](#pythons108) - Empty Block
- [python:S1125](#pythons1125) - Comparison to Boolean
- [python:S1192](#pythons1192) - Duplicate String Literal
- [python:S1481](#pythons1481) - Unused Variable
- [python:S2711](#pythons2711) - Comparison to None
- [python:S2890](#pythons2890) - Global Statement

### INFO

*1 rules*

- [python:S2189](#pythons2189) - Print Statement

## Security Rules

Rules mapped to CWE and OWASP categories for compliance reporting.

### CWE Mapping

| CWE | Description | Rules |
|-----|-------------|-------|
| [CWE-22](https://cwe.mitre.org/data/definitions/22.html) | Path Traversal | `python:S2083`, `javascript:S2083`, `java:S2083` |
| [CWE-73](https://cwe.mitre.org/data/definitions/73.html) | See CWE database | `javascript:S2083` |
| [CWE-78](https://cwe.mitre.org/data/definitions/78.html) | OS Command Injection | `python:S2076`, `javascript:S2076`, `java:S2076`, `go:S2076`, `csharp:S2076`, `php:S2076`, `ruby:S2076` |
| [CWE-79](https://cwe.mitre.org/data/definitions/79.html) | Cross-site Scripting (XSS) | `javascript:S5131`, `php:S5131`, `ruby:S5131` |
| [CWE-89](https://cwe.mitre.org/data/definitions/89.html) | SQL Injection | `python:S3649`, `javascript:S3649`, `java:S3649`, `go:S3649`, `csharp:S3649`, `php:S3649`, `ruby:S3649` |
| [CWE-95](https://cwe.mitre.org/data/definitions/95.html) | Eval Injection | `javascript:S1523` |
| [CWE-98](https://cwe.mitre.org/data/definitions/98.html) | See CWE database | `php:S2083` |
| [CWE-259](https://cwe.mitre.org/data/definitions/259.html) | See CWE database | `python:S2068`, `javascript:S2068`, `java:S2068`, `go:S2068`, `csharp:S2068`, `php:S2068`, `ruby:S2068` |
| [CWE-295](https://cwe.mitre.org/data/definitions/295.html) | Improper Certificate Validation | `go:S4830` |
| [CWE-327](https://cwe.mitre.org/data/definitions/327.html) | Broken Crypto Algorithm | `python:S4790`, `javascript:S5547` |
| [CWE-328](https://cwe.mitre.org/data/definitions/328.html) | Weak Hash | `python:S4790`, `javascript:S5547` |
| [CWE-330](https://cwe.mitre.org/data/definitions/330.html) | Insufficient Randomness | `python:S2245`, `javascript:S2245` |
| [CWE-338](https://cwe.mitre.org/data/definitions/338.html) | Weak PRNG | `python:S2245`, `javascript:S2245` |
| [CWE-390](https://cwe.mitre.org/data/definitions/390.html) | See CWE database | `javascript:S2486` |
| [CWE-391](https://cwe.mitre.org/data/definitions/391.html) | See CWE database | `python:S2737`, `javascript:S2486` |
| [CWE-396](https://cwe.mitre.org/data/definitions/396.html) | See CWE database | `python:S5754` |
| [CWE-400](https://cwe.mitre.org/data/definitions/400.html) | Uncontrolled Resource Consumption | `javascript:S5852` |
| [CWE-502](https://cwe.mitre.org/data/definitions/502.html) | Deserialization | `java:S5135`, `csharp:S5135` |
| [CWE-601](https://cwe.mitre.org/data/definitions/601.html) | Open Redirect | `javascript:S5146` |
| [CWE-611](https://cwe.mitre.org/data/definitions/611.html) | XXE | `java:S2755`, `csharp:S2755` |
| [CWE-614](https://cwe.mitre.org/data/definitions/614.html) | Sensitive Cookie without Secure | `javascript:S2092` |
| [CWE-617](https://cwe.mitre.org/data/definitions/617.html) | See CWE database | `python:S5915` |
| [CWE-798](https://cwe.mitre.org/data/definitions/798.html) | Hard-coded Credentials | `python:S2068`, `javascript:S2068`, `java:S2068`, `go:S2068`, `csharp:S2068`, `php:S2068`, `ruby:S2068` |
| [CWE-915](https://cwe.mitre.org/data/definitions/915.html) | See CWE database | `ruby:S5334` |
| [CWE-918](https://cwe.mitre.org/data/definitions/918.html) | SSRF | `javascript:S5144` |
| [CWE-943](https://cwe.mitre.org/data/definitions/943.html) | See CWE database | `javascript:S5334` |
| [CWE-1004](https://cwe.mitre.org/data/definitions/1004.html) | See CWE database | `javascript:S2092` |
| [CWE-1275](https://cwe.mitre.org/data/definitions/1275.html) | See CWE database | `javascript:S2092` |
| [CWE-1321](https://cwe.mitre.org/data/definitions/1321.html) | See CWE database | `javascript:S5147` |
| [CWE-1333](https://cwe.mitre.org/data/definitions/1333.html) | See CWE database | `javascript:S5852` |

### OWASP Top 10 Mapping

| OWASP | Category | Rules |
|-------|----------|-------|
| A01:2021 | Broken Access Control | `python:S2083`, `javascript:S2083`, `javascript:S5146`, `java:S2083` |
| A02:2021 | Cryptographic Failures | `python:S4790`, `python:S2245`, `javascript:S2245`, `javascript:S5547`, `go:S4830` |
| A03:2021 | Injection | `python:S3649`, `python:S2076`, `javascript:S5131`, `javascript:S1523`, `javascript:S2076`, `javascript:S5147`, `javascript:S3649`, `javascript:S5334`, `java:S3649`, `java:S2076`, `go:S3649`, `go:S2076`, `csharp:S3649`, `csharp:S2076`, `php:S3649`, `php:S2076`, `php:S5131`, `php:S2083`, `ruby:S3649`, `ruby:S2076`, `ruby:S5131` |
| A04:2021 | Insecure Design | `ruby:S5334` |
| A05:2021 | Security Misconfiguration | `javascript:S2092`, `java:S2755`, `csharp:S2755` |
| A06:2021 | Vulnerable Components | `javascript:S5852` |
| A07:2021 | Authentication Failures | `python:S2068`, `javascript:S2068`, `java:S2068`, `go:S2068`, `csharp:S2068`, `php:S2068`, `ruby:S2068` |
| A08:2021 | Software/Data Integrity Failures | `java:S5135`, `csharp:S5135` |
| A10:2021 | SSRF | `javascript:S5144` |

## All Rules

### csharp:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | csharp |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### csharp:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | csharp |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### csharp:S2755

**XML External Entity (XXE)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | csharp |
| Effort | 30 minutes |
| CWE | [CWE-611](https://cwe.mitre.org/data/definitions/611.html) |
| OWASP | A05:2021 |
| Tags | `security`, `xxe`, `xml`, `owasp-top10` |

**Description:**

XML parsers should be configured to prevent XXE attacks

---

### csharp:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | csharp |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10` |

**Description:**

SQL queries should use parameterized queries

---

### csharp:S5135

**Unsafe Deserialization** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | csharp |
| Effort | 60 minutes |
| CWE | [CWE-502](https://cwe.mitre.org/data/definitions/502.html) |
| OWASP | A08:2021 |
| Tags | `security`, `deserialization`, `owasp-top10` |

**Description:**

Deserialization of untrusted data can lead to remote code execution

---

### go:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | go |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### go:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | go |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### go:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | go |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10` |

**Description:**

SQL queries should use parameterized queries

---

### go:S4830

**Insecure TLS Configuration** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | go |
| Effort | 15 minutes |
| CWE | [CWE-295](https://cwe.mitre.org/data/definitions/295.html) |
| OWASP | A02:2021 |
| Tags | `security`, `tls`, `ssl` |

**Description:**

TLS configuration should not disable certificate verification

---

### java:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | java |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### java:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | java |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### java:S2083

**Path Traversal** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | java |
| Effort | 30 minutes |
| CWE | [CWE-22](https://cwe.mitre.org/data/definitions/22.html) |
| OWASP | A01:2021 |
| Tags | `security`, `path-traversal`, `owasp-top10` |

**Description:**

File paths should be validated before use

---

### java:S2755

**XML External Entity (XXE)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | java |
| Effort | 30 minutes |
| CWE | [CWE-611](https://cwe.mitre.org/data/definitions/611.html) |
| OWASP | A05:2021 |
| Tags | `security`, `xxe`, `xml`, `owasp-top10` |

**Description:**

XML parsers should be configured to prevent XXE attacks

---

### java:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | java |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10` |

**Description:**

SQL queries should use PreparedStatement with parameterized queries

---

### java:S5135

**Unsafe Deserialization** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | java |
| Effort | 60 minutes |
| CWE | [CWE-502](https://cwe.mitre.org/data/definitions/502.html) |
| OWASP | A08:2021 |
| Tags | `security`, `deserialization`, `owasp-top10` |

**Description:**

Deserialization of untrusted data can lead to remote code execution

---

### javascript:S107

**Too Many Parameters** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| Tags | `maintainability`, `design` |

**Description:**

Functions should not have too many parameters

---

### javascript:S109

**Magic Number** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 5 minutes |
| Tags | `maintainability`, `readability` |

**Description:**

Magic numbers should be replaced with named constants

---

### javascript:S1186

**Empty Function** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 5 minutes |
| Tags | `maintainability`, `incomplete-code` |

**Description:**

Empty functions should have a comment explaining why they are empty

---

### javascript:S1192

**Duplicated String** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 5 minutes |
| Tags | `maintainability`, `dry` |

**Description:**

String literals should not be duplicated

---

### javascript:S134

**Deep Nesting** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 20 minutes |
| Tags | `maintainability`, `complexity` |

**Description:**

Control flow statements should not be nested too deeply

---

### javascript:S138

**Function Too Long** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| Tags | `maintainability`, `readability` |

**Description:**

Functions should not have too many lines of code

---

### javascript:S1440

**Use Strict Equality** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 2 minutes |
| Tags | `best-practice`, `equality` |

**Description:**

Use strict equality (===) instead of loose equality (==)

---

### javascript:S1481

**Unused Variable** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 2 minutes |
| Tags | `maintainability`, `unused-code` |

**Description:**

Unused variables should be removed

---

### javascript:S1523

**Eval Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-95](https://cwe.mitre.org/data/definitions/95.html) |
| OWASP | A03:2021 |
| Tags | `security`, `injection`, `owasp-top10` |

**Description:**

eval() and similar functions should not be used as they can lead to code injection

---

### javascript:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### javascript:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10`, `nodejs` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### javascript:S2083

**Path Traversal** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-22](https://cwe.mitre.org/data/definitions/22.html), [CWE-73](https://cwe.mitre.org/data/definitions/73.html) |
| OWASP | A01:2021 |
| Tags | `security`, `path-traversal`, `lfi`, `owasp-top10` |

**Description:**

File paths should be validated to prevent directory traversal attacks

---

### javascript:S2092

**Insecure Cookie** 🔓

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 10 minutes |
| CWE | [CWE-614](https://cwe.mitre.org/data/definitions/614.html), [CWE-1004](https://cwe.mitre.org/data/definitions/1004.html), [CWE-1275](https://cwe.mitre.org/data/definitions/1275.html) |
| OWASP | A05:2021 |
| Tags | `security`, `cookie`, `session` |

**Description:**

Cookies should be created with security attributes (Secure, HttpOnly, SameSite)

---

### javascript:S2228

**Console Log** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 2 minutes |
| Tags | `maintainability`, `logging` |

**Description:**

console.log statements should be removed from production code

---

### javascript:S2245

**Insecure Randomness** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 15 minutes |
| CWE | [CWE-338](https://cwe.mitre.org/data/definitions/338.html), [CWE-330](https://cwe.mitre.org/data/definitions/330.html) |
| OWASP | A02:2021 |
| Tags | `security`, `cryptography`, `random` |

**Description:**

Math.random() should not be used for security-sensitive operations

---

### javascript:S2486

**Empty Catch Block** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 15 minutes |
| CWE | [CWE-390](https://cwe.mitre.org/data/definitions/390.html), [CWE-391](https://cwe.mitre.org/data/definitions/391.html) |
| Tags | `error-handling`, `maintainability` |

**Description:**

Catch blocks should not be empty

---

### javascript:S3504

**Avoid var** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 2 minutes |
| Tags | `es6`, `best-practice` |

**Description:**

Variables should be declared with let or const instead of var

---

### javascript:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10` |

**Description:**

SQL queries should use parameterized statements

---

### javascript:S4326

**Async Without Await** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 5 minutes |
| Tags | `async`, `performance` |

**Description:**

Async functions should use await, otherwise they should not be async

---

### javascript:S5131

**Cross-Site Scripting (XSS)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-79](https://cwe.mitre.org/data/definitions/79.html) |
| OWASP | A03:2021 |
| Tags | `security`, `xss`, `owasp-top10` |

**Description:**

User input should be sanitized before being rendered to prevent XSS attacks

---

### javascript:S5144

**SSRF (Server-Side Request Forgery)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-918](https://cwe.mitre.org/data/definitions/918.html) |
| OWASP | A10:2021 |
| Tags | `security`, `ssrf`, `owasp-top10` |

**Description:**

URLs for outbound requests should be validated to prevent SSRF attacks

---

### javascript:S5146

**Open Redirect** 🔓

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 20 minutes |
| CWE | [CWE-601](https://cwe.mitre.org/data/definitions/601.html) |
| OWASP | A01:2021 |
| Tags | `security`, `redirect`, `phishing` |

**Description:**

URLs used for redirects should be validated to prevent phishing attacks

---

### javascript:S5147

**Prototype Pollution** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-1321](https://cwe.mitre.org/data/definitions/1321.html) |
| OWASP | A03:2021 |
| Tags | `security`, `prototype-pollution` |

**Description:**

Object properties should be safely accessed to prevent prototype pollution

---

### javascript:S5334

**NoSQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-943](https://cwe.mitre.org/data/definitions/943.html) |
| OWASP | A03:2021 |
| Tags | `security`, `nosql`, `injection`, `mongodb`, `owasp-top10` |

**Description:**

NoSQL queries should use safe query construction to prevent injection

---

### javascript:S5547

**Weak Cryptography** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| CWE | [CWE-327](https://cwe.mitre.org/data/definitions/327.html), [CWE-328](https://cwe.mitre.org/data/definitions/328.html) |
| OWASP | A02:2021 |
| Tags | `security`, `cryptography` |

**Description:**

Weak cryptographic algorithms should not be used

---

### javascript:S5765

**Callback Hell** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | javascript, typescript |
| Effort | 30 minutes |
| Tags | `async`, `maintainability`, `readability` |

**Description:**

Deeply nested callbacks should be refactored using async/await or Promises

---

### javascript:S5852

**ReDoS (Regex DoS)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | javascript, typescript |
| Effort | 45 minutes |
| CWE | [CWE-1333](https://cwe.mitre.org/data/definitions/1333.html), [CWE-400](https://cwe.mitre.org/data/definitions/400.html) |
| OWASP | A06:2021 |
| Tags | `security`, `regex`, `dos`, `performance` |

**Description:**

Regular expressions should not be vulnerable to catastrophic backtracking

---

### php:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | php |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### php:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | php |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### php:S2083

**File Inclusion** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | php |
| Effort | 30 minutes |
| CWE | [CWE-98](https://cwe.mitre.org/data/definitions/98.html) |
| OWASP | A03:2021 |
| Tags | `security`, `lfi`, `rfi`, `owasp-top10` |

**Description:**

File paths should be validated before inclusion

---

### php:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | php |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10` |

**Description:**

SQL queries should use prepared statements

---

### php:S5131

**Cross-Site Scripting (XSS)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | php |
| Effort | 30 minutes |
| CWE | [CWE-79](https://cwe.mitre.org/data/definitions/79.html) |
| OWASP | A03:2021 |
| Tags | `security`, `xss`, `owasp-top10` |

**Description:**

User input should be escaped before output

---

### python:S107

**Too Many Parameters** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 30 minutes |
| Tags | `maintainability`, `design` |

**Description:**

Functions should not have too many parameters

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `max_params` | `7` |

---

### python:S108

**Empty Block** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 5 minutes |
| Tags | `maintainability`, `readability` |

**Description:**

Empty code blocks should be removed or have a comment explaining why they are empty

---

### python:S1125

**Comparison to Boolean** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 2 minutes |
| Tags | `style`, `readability` |

**Description:**

Comparisons to True/False should be simplified

---

### python:S1192

**Duplicate String Literal** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 5 minutes |
| Tags | `maintainability`, `duplication` |

**Description:**

String literals should not be duplicated

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `min_length` | `10` |
| `min_occurrences` | `3` |

---

### python:S134

**Deep Nesting** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 20 minutes |
| Tags | `maintainability`, `readability`, `complexity` |

**Description:**

Control flow statements should not be nested too deeply

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `max_depth` | `4` |

---

### python:S138

**Function Too Long** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 20 minutes |
| Tags | `maintainability`, `readability` |

**Description:**

Functions should not have too many lines of code

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `max_lines` | `50` |

---

### python:S1481

**Unused Variable** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 2 minutes |
| Tags | `maintainability`, `dead-code` |

**Description:**

Local variables should not be declared and then not used

---

### python:S1541

**Cyclomatic Complexity** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 30 minutes |
| Tags | `maintainability`, `complexity` |

**Description:**

Functions should not have too high cyclomatic complexity

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `threshold` | `10` |

---

### python:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | python |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### python:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | python |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### python:S2083

**Path Traversal** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | python |
| Effort | 30 minutes |
| CWE | [CWE-22](https://cwe.mitre.org/data/definitions/22.html) |
| OWASP | A01:2021 |
| Tags | `security`, `path-traversal`, `owasp-top10` |

**Description:**

File paths should be validated before use to prevent path traversal attacks

---

### python:S2154

**Reassign Builtin** 🐛

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | BUG |
| Languages | python |
| Effort | 5 minutes |
| Tags | `bug`, `python-gotcha` |

**Description:**

Built-in names should not be reassigned

---

### python:S2189

**Print Statement** 👃

| Property | Value |
|----------|-------|
| Severity | ![INFO](https://img.shields.io/badge/-INFO-gray) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 5 minutes |
| Tags | `maintainability`, `logging` |

**Description:**

Consider using logging instead of print statements

---

### python:S2230

**God Class** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 60 minutes |
| Tags | `maintainability`, `design`, `solid` |

**Description:**

Classes should not have too many methods or lines

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `max_methods` | `20` |
| `max_lines` | `500` |

---

### python:S2245

**Insecure Random** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | python |
| Effort | 10 minutes |
| CWE | [CWE-330](https://cwe.mitre.org/data/definitions/330.html), [CWE-338](https://cwe.mitre.org/data/definitions/338.html) |
| OWASP | A02:2021 |
| Tags | `security`, `random`, `cryptography` |

**Description:**

The 'random' module should not be used for security-sensitive operations

---

### python:S2711

**Comparison to None** 🐛

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | BUG |
| Languages | python |
| Effort | 2 minutes |
| Tags | `bug`, `style`, `python-gotcha` |

**Description:**

Comparisons to None should use 'is' or 'is not'

---

### python:S2737

**Except Pass** 🐛

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | BUG |
| Languages | python |
| Effort | 15 minutes |
| CWE | [CWE-391](https://cwe.mitre.org/data/definitions/391.html) |
| Tags | `bug`, `error-handling` |

**Description:**

Except blocks should not just pass silently

---

### python:S2890

**Global Statement** 👃

| Property | Value |
|----------|-------|
| Severity | ![MINOR](https://img.shields.io/badge/-MINOR-blue) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 15 minutes |
| Tags | `maintainability`, `design` |

**Description:**

The 'global' statement should not be used

---

### python:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | python |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10` |

**Description:**

SQL queries should not be constructed from user-controlled data

---

### python:S3776

**Cognitive Complexity** 👃

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | CODE_SMELL |
| Languages | python |
| Effort | 30 minutes |
| Tags | `maintainability`, `complexity`, `readability` |

**Description:**

Cognitive complexity of functions should not be too high

**Parameters:**

| Parameter | Default |
|-----------|---------|
| `threshold` | `15` |

---

### python:S4790

**Weak Cryptographic Hash** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | python |
| Effort | 20 minutes |
| CWE | [CWE-328](https://cwe.mitre.org/data/definitions/328.html), [CWE-327](https://cwe.mitre.org/data/definitions/327.html) |
| OWASP | A02:2021 |
| Tags | `security`, `cryptography`, `hash` |

**Description:**

Weak cryptographic hash functions like MD5 and SHA1 should not be used for security purposes

---

### python:S5754

**Bare Except** 🐛

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | BUG |
| Languages | python |
| Effort | 10 minutes |
| CWE | [CWE-396](https://cwe.mitre.org/data/definitions/396.html) |
| Tags | `bug`, `error-handling` |

**Description:**

Bare 'except:' clauses should not be used

---

### python:S5765

**Return in Finally** 🐛

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | BUG |
| Languages | python |
| Effort | 15 minutes |
| Tags | `bug`, `error-handling` |

**Description:**

Return statements in finally blocks can mask exceptions

---

### python:S5915

**Assert for Validation** 🐛

| Property | Value |
|----------|-------|
| Severity | ![MAJOR](https://img.shields.io/badge/-MAJOR-yellow) |
| Type | BUG |
| Languages | python |
| Effort | 10 minutes |
| CWE | [CWE-617](https://cwe.mitre.org/data/definitions/617.html) |
| Tags | `bug`, `security` |

**Description:**

Assert should not be used for data validation as it can be disabled with -O flag

---

### ruby:S2068

**Hardcoded Credentials** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | ruby |
| Effort | 15 minutes |
| CWE | [CWE-798](https://cwe.mitre.org/data/definitions/798.html), [CWE-259](https://cwe.mitre.org/data/definitions/259.html) |
| OWASP | A07:2021 |
| Tags | `security`, `credentials`, `secrets`, `owasp-top10` |

**Description:**

Credentials should not be hardcoded in source code

---

### ruby:S2076

**Command Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | ruby |
| Effort | 30 minutes |
| CWE | [CWE-78](https://cwe.mitre.org/data/definitions/78.html) |
| OWASP | A03:2021 |
| Tags | `security`, `command-injection`, `owasp-top10` |

**Description:**

OS commands should not be constructed from user-controlled data

---

### ruby:S3649

**SQL Injection** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | ruby |
| Effort | 30 minutes |
| CWE | [CWE-89](https://cwe.mitre.org/data/definitions/89.html) |
| OWASP | A03:2021 |
| Tags | `security`, `sql`, `injection`, `owasp-top10`, `rails` |

**Description:**

SQL queries should use parameterized queries or ActiveRecord safely

---

### ruby:S5131

**Cross-Site Scripting (XSS)** 🔓

| Property | Value |
|----------|-------|
| Severity | ![BLOCKER](https://img.shields.io/badge/-BLOCKER-red) |
| Type | VULNERABILITY |
| Languages | ruby |
| Effort | 30 minutes |
| CWE | [CWE-79](https://cwe.mitre.org/data/definitions/79.html) |
| OWASP | A03:2021 |
| Tags | `security`, `xss`, `owasp-top10`, `rails` |

**Description:**

User input should be sanitized before rendering

---

### ruby:S5334

**Mass Assignment** 🔓

| Property | Value |
|----------|-------|
| Severity | ![CRITICAL](https://img.shields.io/badge/-CRITICAL-orange) |
| Type | VULNERABILITY |
| Languages | ruby |
| Effort | 20 minutes |
| CWE | [CWE-915](https://cwe.mitre.org/data/definitions/915.html) |
| OWASP | A04:2021 |
| Tags | `security`, `mass-assignment`, `rails` |

**Description:**

Models should use strong parameters to prevent mass assignment

---
