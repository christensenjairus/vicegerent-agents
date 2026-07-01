{{- define "vicegerent-mcp.name" -}}
{{- .Values.name | default .Release.Name -}}
{{- end -}}

{{- define "vicegerent-mcp.namespace" -}}
{{- .Values.namespace | default "kmcp" -}}
{{- end -}}
