{{/* ------------------------------------------------------------------ names */}}
{{- define "cairn.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cairn.fullname" -}}
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
{{- define "cairn.componentName" -}}
{{- printf "%s-%s" (include "cairn.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cairn.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ------------------------------------------------------------------ labels */}}
{{- define "cairn.labels" -}}
helm.sh/chart: {{ include "cairn.chart" . }}
app.kubernetes.io/part-of: cairn
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "cairn.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cairn.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "cairn.componentLabels" -}}
{{ include "cairn.labels" .root }}
{{ include "cairn.selectorLabels" . }}
{{- end -}}

{{- define "cairn.serviceAccountName" -}}
{{- if .Values.cairn.serviceAccount.create -}}
{{- default (include "cairn.fullname" .) .Values.cairn.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.cairn.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "cairn.tag" -}}
{{- default .Chart.AppVersion .Values.global.cairn.image.tag -}}
{{- end -}}

{{/* Control-plane image. */}}
{{- define "cairn.image" -}}
{{- $img := .Values.global.cairn.image -}}
{{- printf "%s:%s" (default (printf "%s/platform" .Values.global.cairn.registry) $img.repository) (include "cairn.tag" .) -}}
{{- end -}}

{{/* Cairn image by name: (dict "root" $ "name" "core") */}}
{{- define "cairn.namedImage" -}}
{{- printf "%s/%s:%s" .root.Values.global.cairn.registry .name (include "cairn.tag" .root) -}}
{{- end -}}

{{/* ------------------------------------------------------------------ Cairn Core endpoints */}}
{{- define "cairn.coreHost" -}}
{{- printf "%s-core" (include "cairn.fullname" .) -}}
{{- end -}}

{{- define "cairn.coreUrl" -}}
{{- default (printf "http://%s:8080" (include "cairn.coreHost" .)) .Values.cairn.core.url -}}
{{- end -}}

{{- define "cairn.frontendUrl" -}}
{{- default (printf "http://%s-web:9002" (include "cairn.fullname" .)) .Values.cairn.core.frontendUrl -}}
{{- end -}}

{{- define "cairn.kafkaBootstrap" -}}
{{- default .Values.core.kafka.bootstrap .Values.cairn.kafka.bootstrap -}}
{{- end -}}

{{- define "cairn.schemaRegistryUrl" -}}
{{- default (printf "%s/schema-registry/api/" (include "cairn.coreUrl" .)) .Values.cairn.kafka.schemaRegistryUrl -}}
{{- end -}}

{{/* Secret-backed env entry: (dict "name" ENV "secret" NAME "key" KEY) */}}
{{- define "cairn.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef: { name: {{ .secret }}, key: {{ .key }} }
{{- end -}}

{{/* Settings shared by the core service and the setup job. */}}
{{- define "cairn.coreEnv" -}}
{{- $c := .Values.core -}}
{{- $s := $c.secrets -}}
- name: CAIRN_DB_HOST
  value: {{ printf "%s:%v" $c.database.host $c.database.port | quote }}
- name: CAIRN_DB_NAME
  value: {{ $c.database.name | quote }}
- name: CAIRN_DB_USER
  value: {{ $c.database.user | quote }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_DB_PASSWORD" "secret" $s.name "key" $s.dbPasswordKey) }}
- name: CAIRN_SEARCH_HOST
  value: {{ $c.search.host | quote }}
- name: CAIRN_SEARCH_PORT
  value: {{ $c.search.port | quote }}
- name: CAIRN_SEARCH_USE_SSL
  value: {{ $c.search.useSsl | quote }}
{{- if $c.search.username }}
- name: CAIRN_SEARCH_USERNAME
  value: {{ $c.search.username | quote }}
{{- end }}
{{- if $c.search.passwordKey }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_SEARCH_PASSWORD" "secret" $s.name "key" $c.search.passwordKey) }}
{{- end }}
- name: CAIRN_KAFKA_BOOTSTRAP
  value: {{ $c.kafka.bootstrap | quote }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_SYSTEM_CLIENT_SECRET" "secret" $s.name "key" $s.systemClientSecretKey) }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_SESSION_SIGNING_KEY" "secret" $s.name "key" $s.sessionSigningKeyKey) }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_TOKEN_SALT" "secret" $s.name "key" $s.tokenSaltKey) }}
{{- end -}}

{{- define "cairn.policiesConfigMap" -}}
{{- if .Values.cairn.policies.existingConfigMap -}}
{{- .Values.cairn.policies.existingConfigMap -}}
{{- else if .Values.cairn.policies.files -}}
{{- printf "%s-policies" (include "cairn.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ shared env */}}
{{- define "cairn.env" -}}
{{- $s := .Values.cairn.secrets -}}
- name: CAIRN_CORE_URL
  value: {{ include "cairn.coreUrl" . | quote }}
- name: CAIRN_FRONTEND_URL
  value: {{ include "cairn.frontendUrl" . | quote }}
# Service-account token, written by the core-token bootstrap Job and rotated by the token keeper.
# Mounted from Secret "<release>-core-token"; clients re-read the file when it changes.
- name: CAIRN_CORE_TOKEN_FILE
  value: /run/cairn/core_token
- name: CAIRN_SERVICE_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.serviceTokenKey }} }
- name: CAIRN_DATABASE_URL
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.databaseUrlKey }} }
- name: CAIRN_AUTHZ_MODE
  value: {{ .Values.cairn.auth.mode | quote }}
- name: CAIRN_IMPORT_NATIVE_POLICIES
  value: {{ .Values.cairn.auth.importNativePolicies | quote }}
- name: CAIRN_DECISION_CACHE_TTL
  value: {{ .Values.cairn.auth.decisionCacheTtlSeconds | quote }}
- name: CAIRN_ROOT_USERS
  value: {{ join "," .Values.cairn.auth.rootUsers | quote }}
- name: CAIRN_POLICY_DIR
  value: /etc/cairn/config/policies
- name: CAIRN_KAFKA_BOOTSTRAP
  value: {{ include "cairn.kafkaBootstrap" . | quote }}
- name: CAIRN_SCHEMA_REGISTRY_URL
  value: {{ include "cairn.schemaRegistryUrl" . | quote }}
- name: CAIRN_GATEWAY_PUBLIC_URL
  value: {{ .Values.cairn.gateway.publicUrl | quote }}
- name: CAIRN_GATEWAY_KEY_DIR
  value: /var/lib/cairn/keys
- name: CAIRN_LLM_PROVIDER
  value: {{ .Values.cairn.ai.llmProvider | quote }}
- name: CAIRN_AI_MODEL
  value: {{ .Values.cairn.ai.model | quote }}
- name: CAIRN_AI_EFFORT
  value: {{ .Values.cairn.ai.effort | quote }}
- name: CAIRN_OLLAMA_URL
  value: {{ .Values.cairn.ai.ollamaUrl | quote }}
- name: CAIRN_EMBEDDING_PROVIDER
  value: {{ .Values.cairn.ai.embeddingProvider | quote }}
- name: CAIRN_AI_AUDIT_PROMPTS
  value: {{ .Values.cairn.ai.auditPrompts | quote }}
{{- if eq .Values.cairn.ai.llmProvider "anthropic" }}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.anthropicApiKeyKey }} }
{{- end }}
- name: CAIRN_PUBLIC_URL
  value: {{ .Values.cairn.publicUrl | quote }}
- name: CAIRN_MASKING_PROXY_ENABLED
  value: {{ .Values.cairn.coreProxy.enabled | quote }}
- name: CAIRN_EMBED_ALLOWED_ORIGINS
  value: {{ join "," .Values.cairn.embeds.allowedOrigins | quote }}
- name: CAIRN_ACTION_LINK_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.actionLinkSecretKey }} }
- name: CAIRN_EMBED_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.embedSigningSecretKey }} }
{{- $i := .Values.cairn.integrations }}
{{- if $i.slack.enabled }}
- name: CAIRN_SLACK_BOT_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.botTokenKey }} }
- name: CAIRN_SLACK_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.signingSecretKey }} }
{{- end }}
{{- if $i.teams.enabled }}
- name: CAIRN_TEAMS_WEBHOOK_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.teams.existingSecret }}, key: {{ $i.teams.webhookUrlKey }} }
{{- end }}
{{- if $i.email.enabled }}
- name: CAIRN_SMTP_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.email.existingSecret }}, key: {{ $i.email.smtpUrlKey }} }
{{- end }}
{{- with .Values.cairn.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ deployment
Renders a Cairn Deployment. Arguments (dict):
  root       – top-level context ($)
  component  – component name (api, pdp, gateway, …)
  cfg        – component values
  args       – container args (list) passed to the `cairn` entrypoint
  port       – optional container port (int); enables probes against GET <healthPath>
  healthPath – optional probe path (default /health)
  metricsPath – optional Prometheus scrape path (default /metrics)
  extraEnv   – optional list of extra env entries for this component
  extraVolumes / extraVolumeMounts – optional lists
*/}}
{{- define "cairn.deployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
{{- $replicas := default 1 .replicas -}}
{{- $health := default "/health" .healthPath -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  {{- if not (and $cfg.autoscaling $cfg.autoscaling.enabled) }}
  replicas: {{ $replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "cairn.selectorLabels" . | nindent 6 }}
  {{- if .recreate }}
  strategy:
    type: Recreate
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "cairn.componentLabels" . | nindent 8 }}
      annotations:
        checksum/policies: {{ toYaml $root.Values.cairn.policies | sha256sum | trunc 16 }}
        {{- if .port }}
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ .port | quote }}
        prometheus.io/path: {{ default "/metrics" .metricsPath }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "cairn.serviceAccountName" $root }}
      automountServiceAccountToken: false
      {{- with $root.Values.global.cairn.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.cairn.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ .component }}
          image: {{ include "cairn.image" $root }}
          imagePullPolicy: {{ $root.Values.global.cairn.image.pullPolicy }}
          {{- with .command }}
          command:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          args:
            {{- toYaml .args | nindent 12 }}
          env:
            {{- include "cairn.env" $root | nindent 12 }}
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
            {{- toYaml $root.Values.cairn.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            - name: core-token
              mountPath: /run/cairn
              readOnly: true
            {{- if include "cairn.policiesConfigMap" $root }}
            - name: policies
              mountPath: /etc/cairn/config/policies
              readOnly: true
            {{- end }}
            {{- with .extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 512Mi
        - name: core-token
          secret:
            secretName: {{ include "cairn.coreTokenSecret" $root }}
            optional: true
        {{- if include "cairn.policiesConfigMap" $root }}
        - name: policies
          configMap:
            name: {{ include "cairn.policiesConfigMap" $root }}
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
                    {{- include "cairn.selectorLabels" . | nindent 20 }}
      {{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ service */}}
{{- define "cairn.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
  {{- with .annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: {{ default "ClusterIP" .type }}
  selector:
    {{- include "cairn.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .port }}
      targetPort: http
      protocol: TCP
{{- end -}}

{{/* ------------------------------------------------------------------ hpa / pdb */}}
{{- define "cairn.hpa" -}}
{{- if and .cfg.autoscaling .cfg.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "cairn.componentName" . }}
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

{{- define "cairn.pdb" -}}
{{- if and .cfg.pdb .cfg.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  minAvailable: {{ .cfg.pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "cairn.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ Cairn Core pods
The engine images run as uid/gid 100 and write to their own home (web writes its login file at start),
so the root filesystem stays writable; everything else is locked down.
*/}}
{{- define "cairn.corePodSecurity" -}}
runAsNonRoot: true
runAsUser: 100
runAsGroup: 100
fsGroup: 100
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "cairn.coreContainerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
Cairn Core Deployment. Arguments (dict): root, component, cfg (replicas/resources/scheduling), image
(core|web|executor), env (rendered YAML list), port + healthPath (optional), probeDelay,
volumes / volumeMounts (optional lists).
*/}}
{{- define "cairn.coreDeployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  replicas: {{ default 1 $cfg.replicas }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "cairn.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "cairn.componentLabels" . | nindent 8 }}
    spec:
      automountServiceAccountToken: false
      {{- with $root.Values.global.cairn.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- include "cairn.corePodSecurity" . | nindent 8 }}
      containers:
        - name: {{ .component }}
          image: {{ include "cairn.namedImage" (dict "root" $root "name" .image) }}
          imagePullPolicy: {{ $root.Values.global.cairn.image.pullPolicy }}
          env:
            {{- .env | nindent 12 }}
          {{- if .port }}
          ports:
            - name: http
              containerPort: {{ .port }}
              protocol: TCP
          startupProbe:
            httpGet: { path: {{ .healthPath }}, port: http }
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            httpGet: { path: {{ .healthPath }}, port: http }
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          livenessProbe:
            httpGet: { path: {{ .healthPath }}, port: http }
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 6
          {{- end }}
          resources:
            {{- toYaml $cfg.resources | nindent 12 }}
          securityContext:
            {{- include "cairn.coreContainerSecurity" . | nindent 12 }}
          {{- with .volumeMounts }}
          volumeMounts:
            {{- toYaml . | nindent 12 }}
          {{- end }}
      {{- with .volumes }}
      volumes:
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
      {{- with $cfg.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end -}}

{{- define "cairn.provisionerTokenSecret" -}}
{{- printf "%s-provisioner-token" (include "cairn.fullname" .) -}}
{{- end -}}

{{- define "cairn.coreTokenSecret" -}}
{{- printf "%s-core-token" (include "cairn.fullname" .) -}}
{{- end -}}
