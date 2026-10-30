{{/* Stable names intentionally reject excessive length rather than silently collide. */}}
{{- define "wealthfolio.name" -}}
{{- default .Chart.Name .Values.nameOverride -}}
{{- end -}}
{{- define "wealthfolio.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride -}}
{{- else if contains (include "wealthfolio.name" .) .Release.Name -}}
{{- .Release.Name -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "wealthfolio.name" .) -}}
{{- end -}}
{{- end -}}
{{- define "wealthfolio.selectorLabels" -}}
app.kubernetes.io/name: {{ include "wealthfolio.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "wealthfolio.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{ include "wealthfolio.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}
{{- define "wealthfolio.image" -}}
{{- if .digest -}}
{{- printf "%s:%s@%s" .repository .tag .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}
{{- define "wealthfolio.claimName" -}}
{{- default (include "wealthfolio.fullname" .) .Values.persistence.existingClaim -}}
{{- end -}}
{{- define "wealthfolio.validate" -}}
{{- if ne (int .Values.replicaCount) 1 }}{{ fail "replicaCount must be 1" }}{{ end -}}
{{- if not .Values.auth.existingSecret }}{{ fail "auth.existingSecret is required" }}{{ end -}}
{{- if or (not .Values.auth.secretKeys.secretKey) (not .Values.auth.secretKeys.passwordHash) }}{{ fail "auth.secretKeys must contain both reference key names" }}{{ end -}}
{{- if gt (len (include "wealthfolio.fullname" .)) 53 }}{{ fail "fullnameOverride or release-derived resource name must be at most 53 characters" }}{{ end -}}
{{- if not .Values.image.repository }}{{ fail "image.repository is required; supply the reviewed immutable image reference" }}{{ end -}}
{{- if not .Values.image.digest }}{{ fail "image.digest is required; tag-only application images are unsupported" }}{{ end -}}
{{- if and (eq .Values.image.digest "sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994") (ne .Values.image.tag "3.11.0") }}{{ fail "image.digest does not match the reviewed image.tag" }}{{ end -}}
{{- if and (ne .Values.tests.image.tag "8.23.0") (eq .Values.tests.image.digest "sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93") }}{{ fail "tests.image.digest must be reviewed or cleared when tests.image.tag changes" }}{{ end -}}
{{- if and (ne .Values.service.type "NodePort") .Values.service.nodePort }}{{ fail "service.nodePort requires service.type NodePort" }}{{ end -}}
{{- if and .Values.ingress.enabled (empty .Values.ingress.hosts) }}{{ fail "ingress.hosts is required when ingress.enabled" }}{{ end -}}
{{- range .Values.ingress.tls }}
{{- range .hosts }}{{- if not (has . $.Values.ingress.hosts) }}{{ fail "ingress.tls.hosts must be present in ingress.hosts" }}{{ end }}{{ end -}}
{{- end -}}
{{- if and .Values.mcp.enabled (empty .Values.mcp.allowedHosts) }}{{ fail "mcp.allowedHosts is required when mcp.enabled" }}{{ end -}}
{{- range $map := list .Values.commonLabels .Values.podLabels }}
{{- range $key, $value := $map }}
{{- if or (hasPrefix "app.kubernetes.io/" $key) (hasPrefix "helm.sh/" $key) (hasPrefix "meta.helm.sh/" $key) }}{{ fail "commonLabels and podLabels cannot override reserved metadata keys" }}{{ end -}}
{{- end }}{{ end -}}
{{- range $map := list .Values.podAnnotations .Values.service.annotations .Values.ingress.annotations .Values.persistence.annotations }}
{{- range $key, $value := $map }}
{{- if or (hasPrefix "helm.sh/" $key) (hasPrefix "meta.helm.sh/" $key) }}{{ fail "annotations cannot override reserved Helm metadata keys" }}{{ end -}}
{{- end }}{{ end -}}
{{- range concat .Values.app.corsAllowOrigins .Values.mcp.allowedHosts }}
{{- $literal := regexFind "\\[[0-9A-Fa-f:]+\\]" . -}}
{{- if $literal -}}
{{- $address := trimSuffix "]" (trimPrefix "[" $literal) -}}
{{- $groups := regexFindAll "[0-9A-Fa-f]+" $address -1 -}}
{{- $compressed := contains "::" $address -}}
{{- if or (contains ":::" $address) (gt (len (regexFindAll "::" $address -1)) 1) (and (hasPrefix ":" $address) (not (hasPrefix "::" $address))) (and (hasSuffix ":" $address) (not (hasSuffix "::" $address))) (and $compressed (gt (len $groups) 7)) (and (not $compressed) (ne (len $groups) 8)) }}{{ fail "app.corsAllowOrigins or mcp.allowedHosts contains an invalid IPv6 address" }}{{ end -}}
{{- range $groups }}{{- if gt (len .) 4 }}{{ fail "app.corsAllowOrigins or mcp.allowedHosts contains an invalid IPv6 address" }}{{ end }}{{ end -}}
{{- end -}}
{{- $port := regexFind ":[0-9]+$" . -}}
{{- if and $port (gt (int (trimPrefix ":" $port)) 65535) }}{{ fail "app.corsAllowOrigins or mcp.allowedHosts contains an invalid port" }}{{ end -}}
{{- end -}}
{{- $seen := dict -}}
{{- range .Values.extraEnv }}
{{- if hasKey $seen .name }}{{ fail "extraEnv contains duplicate names" }}{{ end -}}
{{- $_ := set $seen .name true -}}
{{- end -}}
{{- end -}}
