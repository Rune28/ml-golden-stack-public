{{- define "chat-service.labels" -}}
app.kubernetes.io/name: chat-service
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end }}

{{- define "chat-service.selector" -}}
app.kubernetes.io/name: chat-service
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
