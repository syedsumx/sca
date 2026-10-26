{{/* ------------------------------------------------------------------ names */}}
{{- define "aegis.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aegis.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* "<fullname>-<component>" */}}
{{- define "aegis.componentName" -}}
{{- printf "%s-%s" (include "aegis.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aegis.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ------------------------------------------------------------------ labels */}}
{{- define "aegis.labels" -}}
helm.sh/chart: {{ include "aegis.chart" . }}
app.kubernetes.io/part-of: aegis
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "aegis.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aegis.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "aegis.componentLabels" -}}
{{ include "aegis.labels" .root }}
{{ include "aegis.selectorLabels" . }}
{{- end -}}

{{- define "aegis.serviceAccountName" -}}
{{- if .Values.aegis.serviceAccount.create -}}
{{- default (include "aegis.fullname" .) .Values.aegis.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.aegis.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "aegis.image" -}}
{{- $img := .Values.global.aegis.image -}}
{{- printf "%s:%s" $img.repository (default .Chart.AppVersion $img.tag) -}}
{{- end -}}

{{- define "aegis.policiesConfigMap" -}}
{{- if .Values.aegis.policies.existingConfigMap -}}
{{- .Values.aegis.policies.existingConfigMap -}}
{{- else if .Values.aegis.policies.files -}}
{{- printf "%s-policies" (include "aegis.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ shared env */}}
{{- define "aegis.env" -}}
{{- $s := .Values.aegis.secrets -}}
- name: AEGIS_GMS_URL
  value: {{ .Values.aegis.gms.url | quote }}
- name: AEGIS_FRONTEND_URL
  value: {{ .Values.aegis.gms.frontendUrl | quote }}
- name: AEGIS_GMS_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.gmsTokenKey }} }
- name: AEGIS_SERVICE_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.serviceTokenKey }} }
- name: AEGIS_DATABASE_URL
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.databaseUrlKey }} }
- name: AEGIS_AUTHZ_MODE
  value: {{ .Values.aegis.auth.mode | quote }}
- name: AEGIS_IMPORT_NATIVE_POLICIES
  value: {{ .Values.aegis.auth.importNativePolicies | quote }}
- name: AEGIS_DECISION_CACHE_TTL
  value: {{ .Values.aegis.auth.decisionCacheTtlSeconds | quote }}
- name: AEGIS_ROOT_USERS
  value: {{ join "," .Values.aegis.auth.rootUsers | quote }}
- name: AEGIS_POLICY_DIR
  value: /etc/aegis/config/policies
- name: AEGIS_KAFKA_BOOTSTRAP
  value: {{ .Values.aegis.kafka.bootstrap | quote }}
- name: AEGIS_SCHEMA_REGISTRY_URL
  value: {{ .Values.aegis.kafka.schemaRegistryUrl | quote }}
- name: AEGIS_GATEWAY_PUBLIC_URL
  value: {{ .Values.aegis.gateway.publicUrl | quote }}
- name: AEGIS_GATEWAY_KEY_DIR
  value: /var/lib/aegis/keys
- name: AEGIS_LLM_PROVIDER
  value: {{ .Values.aegis.ai.llmProvider | quote }}
- name: AEGIS_AI_MODEL
  value: {{ .Values.aegis.ai.model | quote }}
- name: AEGIS_AI_EFFORT
  value: {{ .Values.aegis.ai.effort | quote }}
- name: AEGIS_OLLAMA_URL
  value: {{ .Values.aegis.ai.ollamaUrl | quote }}
- name: AEGIS_EMBEDDING_PROVIDER
  value: {{ .Values.aegis.ai.embeddingProvider | quote }}
- name: AEGIS_AI_AUDIT_PROMPTS
  value: {{ .Values.aegis.ai.auditPrompts | quote }}
{{- if eq .Values.aegis.ai.llmProvider "anthropic" }}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.anthropicApiKeyKey }} }
{{- end }}
- name: AEGIS_PUBLIC_URL
  value: {{ .Values.aegis.publicUrl | quote }}
- name: AEGIS_MASKING_PROXY_ENABLED
  value: {{ .Values.aegis.gmsProxy.enabled | quote }}
- name: AEGIS_EMBED_ALLOWED_ORIGINS
  value: {{ join "," .Values.aegis.embeds.allowedOrigins | quote }}
- name: AEGIS_ACTION_LINK_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.actionLinkSecretKey }} }
- name: AEGIS_EMBED_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.embedSigningSecretKey }} }
{{- $i := .Values.aegis.integrations }}
{{- if $i.slack.enabled }}
- name: AEGIS_SLACK_BOT_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.botTokenKey }} }
- name: AEGIS_SLACK_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.signingSecretKey }} }
{{- end }}
{{- if $i.teams.enabled }}
- name: AEGIS_TEAMS_WEBHOOK_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.teams.existingSecret }}, key: {{ $i.teams.webhookUrlKey }} }
{{- end }}
{{- if $i.email.enabled }}
- name: AEGIS_SMTP_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.email.existingSecret }}, key: {{ $i.email.smtpUrlKey }} }
{{- end }}
{{- with .Values.aegis.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ deployment
Renders a Aegis Deployment. Arguments (dict):
  root       – top-level context ($)
  component  – component name (api, pdp, gateway, …)
  cfg        – component values
  args       – container args (list) passed to the `aegis` entrypoint
  port       – optional container port (int); enables probes against GET <healthPath>
  healthPath – optional probe path (default /health)
  metricsPath – optional Prometheus scrape path (default /metrics)
  extraEnv   – optional list of extra env entries for this component
  extraVolumes / extraVolumeMounts – optional lists
*/}}
{{- define "aegis.deployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
{{- $replicas := default 1 .replicas -}}
{{- $health := default "/health" .healthPath -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "aegis.componentName" . }}
  labels:
    {{- include "aegis.componentLabels" . | nindent 4 }}
spec:
  {{- if not (and $cfg.autoscaling $cfg.autoscaling.enabled) }}
  replicas: {{ $replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "aegis.selectorLabels" . | nindent 6 }}
  {{- if .recreate }}
  strategy:
    type: Recreate
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "aegis.componentLabels" . | nindent 8 }}
      annotations:
        checksum/policies: {{ toYaml $root.Values.aegis.policies | sha256sum | trunc 16 }}
        {{- if .port }}
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ .port | quote }}
        prometheus.io/path: {{ default "/metrics" .metricsPath }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "aegis.serviceAccountName" $root }}
      automountServiceAccountToken: false
      {{- with $root.Values.global.aegis.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.aegis.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ .component }}
          image: {{ include "aegis.image" $root }}
          imagePullPolicy: {{ $root.Values.global.aegis.image.pullPolicy }}
          args:
            {{- toYaml .args | nindent 12 }}
          env:
            {{- include "aegis.env" $root | nindent 12 }}
            {{- with .extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- if .port }}
          ports:
            - name: http
              containerPort: {{ .port }}
              protocol: TCP
          readinessProbe:
            httpGet: { path: {{ $health }}, port: http }
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            httpGet: { path: {{ $health }}, port: http }
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 4
          startupProbe:
            httpGet: { path: {{ $health }}, port: http }
            periodSeconds: 3
            failureThreshold: 40
          {{- end }}
          resources:
            {{- toYaml $cfg.resources | nindent 12 }}
          securityContext:
            {{- toYaml $root.Values.aegis.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            {{- if include "aegis.policiesConfigMap" $root }}
            - name: policies
              mountPath: /etc/aegis/config/policies
              readOnly: true
            {{- end }}
            {{- with .extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 512Mi
        {{- if include "aegis.policiesConfigMap" $root }}
        - name: policies
          configMap:
            name: {{ include "aegis.policiesConfigMap" $root }}
        {{- end }}
        {{- with .extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- with $cfg.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- if $cfg.affinity }}
      affinity:
        {{- toYaml $cfg.affinity | nindent 8 }}
      {{- else if gt (int $replicas) 1 }}
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                labelSelector:
                  matchLabels:
                    {{- include "aegis.selectorLabels" . | nindent 20 }}
      {{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ service */}}
{{- define "aegis.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "aegis.componentName" . }}
  labels:
    {{- include "aegis.componentLabels" . | nindent 4 }}
  {{- with .annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: {{ default "ClusterIP" .type }}
  selector:
    {{- include "aegis.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .port }}
      targetPort: http
      protocol: TCP
{{- end -}}

{{/* ------------------------------------------------------------------ hpa / pdb */}}
{{- define "aegis.hpa" -}}
{{- if and .cfg.autoscaling .cfg.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "aegis.componentName" . }}
  labels:
    {{- include "aegis.componentLabels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "aegis.componentName" . }}
  minReplicas: {{ .cfg.autoscaling.minReplicas }}
  maxReplicas: {{ .cfg.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .cfg.autoscaling.targetCPUUtilizationPercentage }}
{{- end }}
{{- end -}}

{{- define "aegis.pdb" -}}
{{- if and .cfg.pdb .cfg.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "aegis.componentName" . }}
  labels:
    {{- include "aegis.componentLabels" . | nindent 4 }}
spec:
  minAvailable: {{ .cfg.pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "aegis.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end -}}
