{{- define "dp.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "dp.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: cairn
{{- end -}}

{{- define "dp.connectorSelector" -}}
app.kubernetes.io/name: cairn-dataplane-connector
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "dp.trinoSelector" -}}
app.kubernetes.io/name: cairn-dataplane-trino
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "dp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "dp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "dp.enrollmentSecret" -}}
{{- default (printf "%s-enrollment" (include "dp.fullname" .)) .Values.enrollment.existingSecret -}}
{{- end -}}

{{- define "dp.jobsNamespace" -}}
{{- default .Release.Namespace .Values.jobs.namespace -}}
{{- end -}}

{{- define "dp.connectorService" -}}
{{- printf "%s-connector" (include "dp.fullname" .) -}}
{{- end -}}

{{- define "dp.trinoService" -}}
{{- printf "%s-trino" (include "dp.fullname" .) -}}
{{- end -}}
