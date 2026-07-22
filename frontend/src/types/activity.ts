export type ActivityKind =
  | 'narration'
  | 'agent'
  | 'stage'
  | 'tool'
  | 'skill'
  | 'reasoning'
  | 'status';

export type ActivityState = 'running' | 'completed' | 'error';

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  category: string;
  state: ActivityState;
  agent?: string;
  parentId?: string;
  content: string;
  detail?: string;
  summary?: string;
  durationMs?: number;
  metrics: Record<string, unknown>;
  timestamp: string;
}
