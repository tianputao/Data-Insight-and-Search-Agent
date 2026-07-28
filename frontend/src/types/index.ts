export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
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

export interface SkillInfo {
  name: string;
  description: string;
  tags: string[];
}

export interface ThreadInfo {
  thread_id: string;
  message_count: number;
  last_updated: string;
}

export interface ThreadHistory {
  thread_id: string;
  messages: Array<{
    user: string;
    assistant: string;
    timestamp: string;
  }>;
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
