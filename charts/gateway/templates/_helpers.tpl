{{- define "gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- else if contains (include "gateway.name" .) .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "gateway.name" .) | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "gateway.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: urumi
{{- end -}}

{{- define "gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "gateway.publicUrl" -}}
{{- printf "%s://%s" .Values.gateway.scheme .Values.gateway.host -}}
{{- end -}}

{{/*
One base64 secret value: the value from .Values.secrets if set, else the value
already in the cluster (so upgrades do not rotate it), else a new random one.
Arguments: ctx, name (Secret name), key, value, length
*/}}
{{- define "gateway.secretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace .name -}}
{{- if .value -}}
{{- .value | toString | b64enc -}}
{{- else if and $existing $existing.data (hasKey $existing.data .key) -}}
{{- index $existing.data .key -}}
{{- else -}}
{{- randAlphaNum (int (default 48 .length)) | b64enc -}}
{{- end -}}
{{- end -}}

{{/* Hardening shared by the containers this project builds. */}}
{{- define "gateway.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end -}}

{{/* Argument: uid */}}
{{- define "gateway.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .uid }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Egress rule allowing lookups against the cluster DNS service. */}}
{{- define "gateway.dnsEgress" -}}
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

{{/* A NetworkPolicy peer: the pods of the gateway backend. */}}
{{- define "gateway.gatewayPods" -}}
- podSelector:
    matchLabels:
      {{- include "gateway.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: gateway
{{- end -}}

{{/* A NetworkPolicy peer: the `helm test` pod, which exists only while the test runs. */}}
{{- define "gateway.helmTestPods" -}}
- podSelector:
    matchLabels:
      {{- include "gateway.selectorLabels" . | nindent 6 }}
      urumi.dev/helm-test: "true"
{{- end -}}

{{/* A NetworkPolicy peer: the pods of the ingress controller. */}}
{{- define "gateway.ingressControllerPods" -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ .Values.networkPolicy.ingressController.namespace }}
  podSelector:
    matchLabels:
      {{- toYaml .Values.networkPolicy.ingressController.podLabels | nindent 6 }}
{{- end -}}
