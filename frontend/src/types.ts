// Type definitions for Azure Doc Agent frontend

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  tags: string[];
}

export interface SessionInfo {
  id: string;
  name: string;
  created_at: string;
  message_count: number;
}

export interface ChatRequest {
  message: string;
  thread_id?: string;
  enable_ontology?: boolean;
}

export interface ChatResponse {
  response: string;
  thread_id: string;
  timestamp: string;
}

export interface RuntimeConfig {
  default_enable_ontology: boolean;
  ontology: {
    available: boolean;
    reasoner_enabled?: boolean;
    reasoner?: string;
    reasoning_status: string;
    reasoning_error?: string | null;
    file_count?: number;
    ontology_count?: number;
    entity_count?: number;
  };
}
