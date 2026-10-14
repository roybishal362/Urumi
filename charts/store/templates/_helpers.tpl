{{- define "store.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{/*
Resource name prefix. A release called "store" gives "store-wordpress"; a
release called "store-abc" gives "store-abc-wordpress".
*/}}
{{- define "store.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- else if contains (include "store.name" .) .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "store.name" .) | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "store.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "store.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: urumi
{{- end -}}

{{- define "store.selectorLabels" -}}
app.kubernetes.io/name: {{ include "store.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* The URL customers use, for example http://store.localtest.me */}}
{{- define "store.publicUrl" -}}
{{- printf "%s://%s" .Values.store.scheme .Values.store.host -}}
{{- end -}}

{{/* In-cluster URL of WordPress. Only the WooCommerce MCP server uses it. */}}
{{- define "store.internalUrl" -}}
{{- printf "http://%s-wordpress.%s.svc.cluster.local" (include "store.fullname" .) .Release.Namespace -}}
{{- end -}}

{{/*
One base64 secret value. Order of precedence:
  1. the value from .Values.secrets, if set
  2. the value already stored in the cluster (so upgrades do not rotate it)
  3. a new random value
Arguments: ctx, name (Secret name), key, value, prefix, length
*/}}
{{- define "store.secretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace .name -}}
{{- if .value -}}
{{- .value | toString | b64enc -}}
{{- else if and $existing $existing.data (hasKey $existing.data .key) -}}
{{- index $existing.data .key -}}
{{- else -}}
{{- printf "%s%s" (default "" .prefix) (randAlphaNum (int (default 32 .length))) | b64enc -}}
{{- end -}}
{{- end -}}

{{/*
Environment shared by the WordPress container and the WP-CLI container. Both
load the same wp-config.php, which reads these at runtime.
*/}}
{{- define "store.wordpressEnv" -}}
- name: WORDPRESS_DB_HOST
  value: {{ printf "%s-mariadb:3306" (include "store.fullname" .) | quote }}
- name: WORDPRESS_DB_NAME
  value: wordpress
- name: WORDPRESS_DB_USER
  value: wordpress
- name: WORDPRESS_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "store.fullname" . }}-db
      key: mariadb-password
- name: WORDPRESS_CONFIG_EXTRA
  value: |
    define('WP_HOME', '{{ include "store.publicUrl" . }}');
    define('WP_SITEURL', '{{ include "store.publicUrl" . }}');
    define('AUTOMATIC_UPDATER_DISABLED', true);
{{- end -}}

{{- define "store.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
{{- end -}}

{{/* Egress rule allowing lookups against the cluster DNS service. */}}
{{- define "store.dnsEgress" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end -}}
