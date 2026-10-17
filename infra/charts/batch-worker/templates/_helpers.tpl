{{- define "batch-worker.labels" -}}
app.kubernetes.io/name: batch-worker
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end }}

{{- define "batch-worker.selector" -}}
app.kubernetes.io/name: batch-worker
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
