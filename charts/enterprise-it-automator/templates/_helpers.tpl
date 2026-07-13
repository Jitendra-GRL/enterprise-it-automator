{{- define "eit.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "eit.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "eit.labels" -}}
app.kubernetes.io/name: {{ include "eit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "eit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "eit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
