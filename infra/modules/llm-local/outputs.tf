# The OpenAI-compatible API of the server (/v1/chat/completions,
# /v1/embeddings), which Ollama, llama.cpp and vLLM all serve.
output "base_url" {
  value = "http://${kubernetes_service_v1.this.metadata[0].name}.${kubernetes_namespace_v1.this.metadata[0].name}.svc/v1"
}
