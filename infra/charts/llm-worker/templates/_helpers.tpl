{{- define "llm-worker.labels" -}}
app.kubernetes.io/name: llm-worker
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end }}

{{- define "llm-worker.selector" -}}
app.kubernetes.io/name: llm-worker
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
