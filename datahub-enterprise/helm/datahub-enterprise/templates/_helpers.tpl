{{/* ------------------------------------------------------------------ names */}}
{{- define "dhe.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "dhe.fullname" -}}
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
{{- define "dhe.componentName" -}}
{{- printf "%s-%s" (include "dhe.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "dhe.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ------------------------------------------------------------------ labels */}}
{{- define "dhe.labels" -}}
helm.sh/chart: {{ include "dhe.chart" . }}
app.kubernetes.io/part-of: datahub-enterprise
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "dhe.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dhe.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "dhe.componentLabels" -}}
{{ include "dhe.labels" .root }}
{{ include "dhe.selectorLabels" . }}
{{- end -}}

{{- define "dhe.serviceAccountName" -}}
{{- if .Values.dhe.serviceAccount.create -}}
{{- default (include "dhe.fullname" .) .Values.dhe.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.dhe.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "dhe.image" -}}
{{- $img := .Values.global.dhe.image -}}
{{- printf "%s:%s" $img.repository (default .Chart.AppVersion $img.tag) -}}
{{- end -}}

{{- define "dhe.policiesConfigMap" -}}
{{- if .Values.dhe.policies.existingConfigMap -}}
{{- .Values.dhe.policies.existingConfigMap -}}
{{- else if .Values.dhe.policies.files -}}
{{- printf "%s-policies" (include "dhe.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ shared env */}}
{{- define "dhe.env" -}}
{{- $s := .Values.dhe.secrets -}}
- name: DHE_GMS_URL
  value: {{ .Values.dhe.gms.url | quote }}
- name: DHE_FRONTEND_URL
  value: {{ .Values.dhe.gms.frontendUrl | quote }}
- name: DHE_GMS_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.gmsTokenKey }} }
- name: DHE_SERVICE_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.serviceTokenKey }} }
- name: DHE_DATABASE_URL
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.databaseUrlKey }} }
- name: DHE_AUTHZ_MODE
  value: {{ .Values.dhe.auth.mode | quote }}
- name: DHE_IMPORT_NATIVE_POLICIES
  value: {{ .Values.dhe.auth.importNativePolicies | quote }}
- name: DHE_DECISION_CACHE_TTL
  value: {{ .Values.dhe.auth.decisionCacheTtlSeconds | quote }}
- name: DHE_ROOT_USERS
  value: {{ join "," .Values.dhe.auth.rootUsers | quote }}
- name: DHE_POLICY_DIR
  value: /etc/dhe/config/policies
- name: DHE_KAFKA_BOOTSTRAP
  value: {{ .Values.dhe.kafka.bootstrap | quote }}
- name: DHE_SCHEMA_REGISTRY_URL
  value: {{ .Values.dhe.kafka.schemaRegistryUrl | quote }}
- name: DHE_GATEWAY_PUBLIC_URL
  value: {{ .Values.dhe.gateway.publicUrl | quote }}
- name: DHE_GATEWAY_KEY_DIR
  value: /var/lib/dhe/keys
- name: DHE_LLM_PROVIDER
  value: {{ .Values.dhe.ai.llmProvider | quote }}
- name: DHE_AI_MODEL
  value: {{ .Values.dhe.ai.model | quote }}
- name: DHE_AI_EFFORT
  value: {{ .Values.dhe.ai.effort | quote }}
- name: DHE_OLLAMA_URL
  value: {{ .Values.dhe.ai.ollamaUrl | quote }}
- name: DHE_EMBEDDING_PROVIDER
  value: {{ .Values.dhe.ai.embeddingProvider | quote }}
- name: DHE_AI_AUDIT_PROMPTS
  value: {{ .Values.dhe.ai.auditPrompts | quote }}
{{- if eq .Values.dhe.ai.llmProvider "anthropic" }}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.anthropicApiKeyKey }} }
{{- end }}
- name: DHE_PUBLIC_URL
  value: {{ .Values.dhe.publicUrl | quote }}
- name: DHE_MASKING_PROXY_ENABLED
  value: {{ .Values.dhe.gmsProxy.enabled | quote }}
- name: DHE_EMBED_ALLOWED_ORIGINS
  value: {{ join "," .Values.dhe.embeds.allowedOrigins | quote }}
- name: DHE_ACTION_LINK_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.actionLinkSecretKey }} }
- name: DHE_EMBED_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.embedSigningSecretKey }} }
{{- $i := .Values.dhe.integrations }}
{{- if $i.slack.enabled }}
- name: DHE_SLACK_BOT_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.botTokenKey }} }
- name: DHE_SLACK_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.signingSecretKey }} }
{{- end }}
{{- if $i.teams.enabled }}
- name: DHE_TEAMS_WEBHOOK_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.teams.existingSecret }}, key: {{ $i.teams.webhookUrlKey }} }
{{- end }}
{{- if $i.email.enabled }}
- name: DHE_SMTP_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.email.existingSecret }}, key: {{ $i.email.smtpUrlKey }} }
{{- end }}
{{- with .Values.dhe.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ deployment
Renders a DHE Deployment. Arguments (dict):
  root       – top-level context ($)
  component  – component name (api, pdp, gateway, …)
  cfg        – component values
  args       – container args (list) passed to the `dhe` entrypoint
  port       – optional container port (int); enables probes against GET <healthPath>
  healthPath – optional probe path (default /health)
  metricsPath – optional Prometheus scrape path (default /metrics)
  extraEnv   – optional list of extra env entries for this component
  extraVolumes / extraVolumeMounts – optional lists
*/}}
{{- define "dhe.deployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
{{- $replicas := default 1 .replicas -}}
{{- $health := default "/health" .healthPath -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "dhe.componentName" . }}
  labels:
    {{- include "dhe.componentLabels" . | nindent 4 }}
spec:
  {{- if not (and $cfg.autoscaling $cfg.autoscaling.enabled) }}
  replicas: {{ $replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "dhe.selectorLabels" . | nindent 6 }}
  {{- if .recreate }}
  strategy:
    type: Recreate
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "dhe.componentLabels" . | nindent 8 }}
      annotations:
        checksum/policies: {{ toYaml $root.Values.dhe.policies | sha256sum | trunc 16 }}
        {{- if .port }}
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ .port | quote }}
        prometheus.io/path: {{ default "/metrics" .metricsPath }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "dhe.serviceAccountName" $root }}
      automountServiceAccountToken: false
      {{- with $root.Values.global.dhe.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.dhe.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ .component }}
          image: {{ include "dhe.image" $root }}
          imagePullPolicy: {{ $root.Values.global.dhe.image.pullPolicy }}
          args:
            {{- toYaml .args | nindent 12 }}
          env:
            {{- include "dhe.env" $root | nindent 12 }}
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
            {{- toYaml $root.Values.dhe.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            {{- if include "dhe.policiesConfigMap" $root }}
            - name: policies
              mountPath: /etc/dhe/config/policies
              readOnly: true
            {{- end }}
            {{- with .extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 512Mi
        {{- if include "dhe.policiesConfigMap" $root }}
        - name: policies
          configMap:
            name: {{ include "dhe.policiesConfigMap" $root }}
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
                    {{- include "dhe.selectorLabels" . | nindent 20 }}
      {{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ service */}}
{{- define "dhe.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "dhe.componentName" . }}
  labels:
    {{- include "dhe.componentLabels" . | nindent 4 }}
  {{- with .annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: {{ default "ClusterIP" .type }}
  selector:
    {{- include "dhe.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .port }}
      targetPort: http
      protocol: TCP
{{- end -}}

{{/* ------------------------------------------------------------------ hpa / pdb */}}
{{- define "dhe.hpa" -}}
{{- if and .cfg.autoscaling .cfg.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "dhe.componentName" . }}
  labels:
    {{- include "dhe.componentLabels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "dhe.componentName" . }}
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

{{- define "dhe.pdb" -}}
{{- if and .cfg.pdb .cfg.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "dhe.componentName" . }}
  labels:
    {{- include "dhe.componentLabels" . | nindent 4 }}
spec:
  minAvailable: {{ .cfg.pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "dhe.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end -}}
