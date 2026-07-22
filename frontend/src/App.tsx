import React, { useState, useEffect, useRef } from 'react';
import { apiService } from './services/api';
import type { ChatMessage, SessionInfo } from './types';
import './styles/global.css';
import './styles/App.css';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ActivityPanel } from './components/ActivityPanel';
import type { ActivityItem, ActivityKind, ActivityState } from './types/activity';

// Example questions from the enterprise knowledge base (matching app.py EXAMPLE_QUESTIONS)
const EXAMPLE_QUERIES = [
  "汽车用液化天然气的加液口基本构型",
  "电动汽车用动力蓄电池安全要求",
  "What are the recall criteria for defective automotive products",
  "什么是management body, 它在乘用车法规里做什么用的，目前发行了几个版本",
  "哪个客户在2023年的消费是最高的",
  "按月看2023年的销售额趋势",
  "按产品类别看2023的销量"
];

interface MessageWithThinking extends ChatMessage {
  thinking?: ActivityItem[];
  thinkingCollapsed?: boolean;
}

const EMPTY_MESSAGES: MessageWithThinking[] = [];

const normalizeReferenceUrl = (rawUrl: string): string => {
  if (!rawUrl) return '';
  let candidate = rawUrl.trim().replace(/^<|>$/g, '').trim();
  if (!candidate) return '';

  const firstToken = candidate.split(/\s+/)[0]?.trim() || '';
  candidate = firstToken.replace(/^<|>$/g, '').replace(/[.,;]+$/g, '');

  if (!/^https?:\/\//i.test(candidate)) return '';
  return candidate;
};

const isGenericReferenceTitle = (title: string): boolean => {
  const normalized = (title || '').trim();
  if (!normalized) return true;
  if (/^Reference\s+\d+$/i.test(normalized)) return true;
  if (/^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(\.pdf)?$/.test(normalized)) return true;
  if (/^[\w-]+\/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(\.pdf)?$/.test(normalized)) return true;
  return false;
};

const deriveTitleFromUrl = (num: string, title: string, url: string): string => {
  const normalizedTitle = (title || '').trim();
  if (normalizedTitle && !isGenericReferenceTitle(normalizedTitle)) {
    return normalizedTitle;
  }

  if (url) {
    try {
      const pathname = new URL(url).pathname;
      const fileName = decodeURIComponent(pathname.split('/').pop() || '').trim();
      if (fileName) return fileName;
    } catch {
      // ignore and fallback
    }
  }

  return normalizedTitle || `Reference ${num}`;
};

const referenceGroupKeys = (url: string): string[] => {
  const cleanUrl = normalizeReferenceUrl(url);
  if (!cleanUrl) return [];

  try {
    const parsed = new URL(cleanUrl);
    const noFragment = `${parsed.origin}${parsed.pathname}${parsed.search}`;
    const noQueryNoFragment = `${parsed.origin}${parsed.pathname}`;
    const baseName = decodeURIComponent(parsed.pathname.split('/').pop() || '').trim();
    const stem = baseName.replace(/\.[^.]+$/, '');
    return [noFragment, noQueryNoFragment, stem].filter(Boolean);
  } catch {
    return [cleanUrl.split('#')[0]];
  }
};

export const repairCollapsedMarkdownTables = (content: string): string => {
  if (!content || !content.includes('|')) return content;

  const repairedLines: string[] = [];
  let inCodeFence = false;
  const separatorCellPattern = /\|\s*:?-{3,}:?\s*\|/;
  const rowBoundaryPattern = /\|\s*\|/g;

  for (const line of content.split('\n')) {
    if (line.trimStart().startsWith('```')) {
      inCodeFence = !inCodeFence;
      repairedLines.push(line);
      continue;
    }

    const pipeCount = (line.match(/\|/g) || []).length;
    if (!inCodeFence && pipeCount >= 6 && separatorCellPattern.test(line)) {
      repairedLines.push(...line.replace(rowBoundaryPattern, '|\n|').split('\n'));
    } else {
      repairedLines.push(line);
    }
  }

  return repairedLines.join('\n');
};

const normalizeCitationsForDisplay = (content: string): string => {
  if (!content) return content;

  let body = content;
  let refsText = '';

  if (content.includes('References:')) {
    const parts = content.split('References:');
    body = parts[0] || '';
    refsText = parts.slice(1).join('References:') || '';
  } else {
    const implicitRefStart = content.search(/\n\s*\[\d+\]\s*\[.*?\]\(https?:\/\/[^)]+\)/s);
    if (implicitRefStart >= 0) {
      body = content.slice(0, implicitRefStart);
      refsText = content.slice(implicitRefStart);
    }
  }

  const refsMap = new Map<string, { title: string; url: string }>();
  for (const m of refsText.matchAll(/\[(\d+)\]\s*\[(.*?)\]\(([^)]+)\)/g)) {
    const num = m[1];
    const url = normalizeReferenceUrl(m[3]);
    const title = (m[2] || '').trim();
    if (url) {
      refsMap.set(num, { title: deriveTitleFromUrl(num, title, url), url });
    }
  }

  for (const m of refsText.matchAll(/\[(\d+)\]\s+([^\n\[][^\n]*)/g)) {
    const num = m[1];
    if (!refsMap.has(num)) {
      refsMap.set(num, { title: (m[2] || '').trim() || `Reference ${num}`, url: '' });
    }
  }

  for (const m of body.matchAll(/\[\[(\d+)\]\]\(([^)]+)\)/g)) {
    const num = m[1];
    const url = normalizeReferenceUrl(m[2]);
    if (!refsMap.has(num) && url) {
      refsMap.set(num, { title: deriveTitleFromUrl(num, '', url), url });
    }
  }

  const keyBestTitle = new Map<string, string>();
  refsMap.forEach((item) => {
    const normalizedTitle = (item.title || '').trim();
    if (!normalizedTitle || isGenericReferenceTitle(normalizedTitle)) return;
    referenceGroupKeys(item.url).forEach((key) => {
      if (!keyBestTitle.has(key)) keyBestTitle.set(key, normalizedTitle);
    });
  });

  refsMap.forEach((item, num) => {
    const normalizedTitle = (item.title || '').trim();
    if (normalizedTitle && !isGenericReferenceTitle(normalizedTitle)) return;
    for (const key of referenceGroupKeys(item.url)) {
      const better = keyBestTitle.get(key);
      if (better) {
        refsMap.set(num, { ...item, title: better });
        break;
      }
    }
  });

  const citedOrder: string[] = [];
  for (const m of body.matchAll(/\[\[(\d+)\]\]/g)) {
    if (!citedOrder.includes(m[1])) citedOrder.push(m[1]);
  }

  const remaining = Array.from(refsMap.keys()).filter(k => !citedOrder.includes(k));
  const mergedOrder = [...citedOrder, ...remaining];
  if (mergedOrder.length === 0) {
    return body.trimEnd();
  }

  const remap = new Map<string, string>();
  mergedOrder.forEach((oldNum, idx) => remap.set(oldNum, String(idx + 1)));

  let normalizedBody = body.replace(/\[\[(\d+)\]\]\(([^)]+)\)/g, (_all, oldNum, rawUrl) => {
    const newNum = remap.get(oldNum) || oldNum;
    const cleanUrl = normalizeReferenceUrl(rawUrl) || rawUrl;
    return `[[${newNum}]](${cleanUrl})`;
  });
  normalizedBody = normalizedBody.replace(/\[\[(\d+)\]\]/g, (_all, oldNum) => {
    const newNum = remap.get(oldNum) || oldNum;
    return `[[${newNum}]]`;
  });

  const refLines = mergedOrder.map(oldNum => {
    const item = refsMap.get(oldNum);
    const newNum = remap.get(oldNum) || oldNum;
    if (!item) return '';
    const cleanUrl = normalizeReferenceUrl(item.url);
    const resolvedTitle = deriveTitleFromUrl(newNum, item.title, cleanUrl);
    if (cleanUrl && /^https?:\/\//.test(cleanUrl)) {
      return `[${newNum}] [${resolvedTitle}](${cleanUrl})`;
    }
    return `[${newNum}] ${resolvedTitle}`;
  }).filter(Boolean);

  if (refLines.length === 0) {
    return normalizedBody.trimEnd();
  }

  return `${normalizedBody.trimEnd()}\n\nReferences:\n${refLines.join('\n\n')}`;
};

// ── Stable component for proxied blob images (hooks must live in a named component) ──
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');
const apiUrl = (path: string) => `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;

const ProxiedImage: React.FC<{ src?: string; alt?: string }> = ({ src, alt }) => {
  const [failed, setFailed] = React.useState(false);

  const normalizedSrc = React.useMemo(() => {
    if (!src) return '';
    return src.trim().replace(/^<|>$/g, '');
  }, [src]);

  const resolvedSrc = React.useMemo(() => {
    if (!normalizedSrc) return normalizedSrc;
    if (normalizedSrc.includes('/proxy-image?url=')) {
      return normalizedSrc;
    }
    if (normalizedSrc.includes('blob.core.windows.net')) {
      return apiUrl(`/proxy-image?url=${encodeURIComponent(normalizedSrc)}`);
    }
    return normalizedSrc;
  }, [normalizedSrc]);

  if (!normalizedSrc) return null;

  if (failed) {
    return (
      <a href={normalizedSrc} target="_blank" rel="noopener noreferrer" className="img-fallback-link">
        🖼️ {alt || '查看图片'}
      </a>
    );
  }

  return (
    <img
      src={resolvedSrc}
      alt={alt || ''}
      className="msg-image"
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
};

function App() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionCounter, setSessionCounter] = useState(1);
  const [autoScroll, setAutoScroll] = useState(true);
  const [sessionMessages, setSessionMessages] = useState<Map<string, MessageWithThinking[]>>(new Map());
  const [loadingSessionIds, setLoadingSessionIds] = useState<Set<string>>(new Set());
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  const initialSessionRequestedRef = useRef(false);

  const messages = sessionMessages.get(currentSessionId) ?? EMPTY_MESSAGES;
  const isLoading = loadingSessionIds.has(currentSessionId);

  const updateSessionMessages = (
    sessionId: string,
    updater: (previous: MessageWithThinking[]) => MessageWithThinking[]
  ) => {
    setSessionMessages(previous => {
      const next = new Map(previous);
      next.set(sessionId, updater(next.get(sessionId) ?? []));
      return next;
    });
  };

  useEffect(() => {
    // Create the initial session on mount
    if (sessions.length === 0 && !initialSessionRequestedRef.current) {
      initialSessionRequestedRef.current = true;
      createInitialSession();
    }
  }, []);

  useEffect(() => {
    if (chatContainerRef.current && autoScroll) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
    }
  }, [messages, autoScroll]);

  // 监听用户滚动
  useEffect(() => {
    const container = chatContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      const isNearBottom = scrollHeight - scrollTop - clientHeight < 100;
      setAutoScroll(isNearBottom);
    };

    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, []);

  const createInitialSession = async () => {
    try {
      const result = await apiService.createThread();
      const newSession: SessionInfo = {
        id: result.thread_id,
        name: 'Session 1',
        created_at: new Date().toISOString(),
        message_count: 0
      };
      setCurrentSessionId(result.thread_id);
      setSessions([newSession]);
      setSessionCounter(2);
      setSessionMessages(new Map([[result.thread_id, []]]));
    } catch (error) {
      initialSessionRequestedRef.current = false;
      console.error('Failed to create initial session:', error);
    }
  };

  const createNewSession = async () => {
    try {
      const result = await apiService.createThread();
      const newSession: SessionInfo = {
        id: result.thread_id,
        name: `Session ${sessionCounter}`,
        created_at: new Date().toISOString(),
        message_count: 0
      };
      setCurrentSessionId(result.thread_id);
      setSessions(prev => [...prev, newSession]);
      setSessionCounter(prev => prev + 1);
      setSessionMessages(prev => new Map(prev).set(result.thread_id, []));
    } catch (error) {
      console.error('Failed to create session:', error);
    }
  };

  const switchSession = (sessionId: string) => {
    setCurrentSessionId(sessionId);
    setAutoScroll(true);
  };

  const deleteSession = async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    
    // 如果只剩一个session，不允许删除
    if (sessions.length === 1) {
      alert('至少需要保留一个会话');
      return;
    }

    try {
      abortControllersRef.current.get(sessionId)?.abort();
      abortControllersRef.current.delete(sessionId);
      await apiService.stopThread(sessionId).catch(() => undefined);
      await apiService.deleteThread(sessionId);

      // 删除session的消息记录
      setSessionMessages(prev => {
        const newMap = new Map(prev);
        newMap.delete(sessionId);
        return newMap;
      });

      setSessions(prev => prev.filter(s => s.id !== sessionId));
      setLoadingSessionIds(previous => {
        const next = new Set(previous);
        next.delete(sessionId);
        return next;
      });
      
      // 如果删除的是当前session，切换到第一个session
      if (currentSessionId === sessionId) {
        const remainingSessions = sessions.filter(s => s.id !== sessionId);
        if (remainingSessions.length > 0) {
          setCurrentSessionId(remainingSessions[0].id);
        }
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
    }
  };

  const sendMessageStream = async () => {
    if (!inputValue.trim() || isLoading) return;

    const sessionId = currentSessionId;
    if (!sessionId) return;

    const userMessage: MessageWithThinking = {
      role: 'user',
      content: inputValue,
      timestamp: new Date().toISOString()
    };

    const currentInput = inputValue;
    setInputValue('');
    setLoadingSessionIds(previous => new Set(previous).add(sessionId));
    setAutoScroll(true); // 开始新消息时启用自动滚动

    // 先创建一个空的assistant消息框架，thinking在前面
    const initialAssistantMessage: MessageWithThinking = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      thinking: [],
      thinkingCollapsed: false
    };
    updateSessionMessages(sessionId, previous => [
      ...previous,
      userMessage,
      initialAssistantMessage,
    ]);

    const abortController = new AbortController();
    abortControllersRef.current.set(sessionId, abortController);

    try {
      const response = await fetch(apiUrl('/chat/stream'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          message: currentInput,
          thread_id: sessionId
        }),
        signal: abortController.signal,
      });

      if (!response.ok) {
        const errBody = await response.text().catch(() => '');
        throw new Error(`Failed to send message (${response.status}): ${errBody || response.statusText}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let assistantContent = '';
      let thinkingForMessage: ActivityItem[] = [];
      let sseBuffer = '';
      let activitySequence = 0;
      let didFinalize = false;

      const updateAssistantMessage = (content: string, thinking: ActivityItem[]) => {
        updateSessionMessages(sessionId, prev => {
          const newMessages = [...prev];
          const lastMessage = newMessages[newMessages.length - 1];
          if (lastMessage && lastMessage.role === 'assistant') {
            newMessages[newMessages.length - 1] = {
              ...lastMessage,
              content,
              thinking: [...thinking]
            };
          }
          return newMessages;
        });
      };

      const upsertThinking = (data: Record<string, unknown>) => {
        const content = typeof data.message === 'string' ? data.message.trim() : '';
        if (!content) return;

        const id = typeof data.id === 'string' && data.id
          ? data.id
          : `activity-${++activitySequence}`;
        const supportedKinds: ActivityKind[] = ['narration', 'agent', 'stage', 'tool', 'skill', 'reasoning', 'status'];
        const kind: ActivityKind = typeof data.kind === 'string' && supportedKinds.includes(data.kind as ActivityKind)
          ? data.kind as ActivityKind
          : 'tool';
        const state: ActivityState = data.state === 'completed' || data.state === 'error'
          ? data.state
          : 'running';
        const existingIndex = thinkingForMessage.findIndex(item => item.id === id);

        if (existingIndex >= 0) {
          const existing = thinkingForMessage[existingIndex];
          const nextContent = data.append === true ? existing.content + content : content;
          thinkingForMessage = thinkingForMessage.map((item, index) => index === existingIndex
            ? {
                ...item,
                content: nextContent,
                state,
                category: typeof data.category === 'string' ? data.category : item.category,
                agent: typeof data.agent === 'string' ? data.agent : item.agent,
                parentId: typeof data.parent_id === 'string' ? data.parent_id : item.parentId,
                detail: typeof data.detail === 'string' ? data.detail : item.detail,
                summary: typeof data.summary === 'string' ? data.summary : item.summary,
                durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : item.durationMs,
                metrics: typeof data.metrics === 'object' && data.metrics !== null
                  ? data.metrics as Record<string, unknown>
                  : item.metrics
              }
            : item);
        } else {
          thinkingForMessage = [...thinkingForMessage, {
            id,
            kind,
            category: typeof data.category === 'string' ? data.category : kind,
            state,
            agent: typeof data.agent === 'string' ? data.agent : undefined,
            parentId: typeof data.parent_id === 'string' ? data.parent_id : undefined,
            content,
            detail: typeof data.detail === 'string' ? data.detail : undefined,
            summary: typeof data.summary === 'string' ? data.summary : undefined,
            durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : undefined,
            metrics: typeof data.metrics === 'object' && data.metrics !== null
              ? data.metrics as Record<string, unknown>
              : {},
            timestamp: new Date().toISOString()
          }];
        }

        updateAssistantMessage(assistantContent, thinkingForMessage);
      };

      const completeThinking = () => {
        thinkingForMessage = thinkingForMessage.map(item => item.state === 'running'
          ? { ...item, state: 'completed' }
          : item);
        updateAssistantMessage(assistantContent, thinkingForMessage);
      };

      const finalizeAssistantMessage = (finalContent?: string) => {
        if (typeof finalContent === 'string' && finalContent.trim()) {
          assistantContent = finalContent;
        }

        thinkingForMessage = thinkingForMessage.map(item => item.state === 'running'
          ? { ...item, state: 'completed' }
          : item);
        assistantContent = normalizeCitationsForDisplay(
          repairCollapsedMarkdownTables(assistantContent)
        );
        didFinalize = true;

        updateSessionMessages(sessionId, prev => {
          const newMessages = [...prev];
          const lastMsg = newMessages[newMessages.length - 1];
          if (lastMsg && lastMsg.role === 'assistant') {
            newMessages[newMessages.length - 1] = {
              ...lastMsg,
              content: assistantContent,
              thinking: [...thinkingForMessage],
              thinkingCollapsed: true
            };
          }
          return newMessages;
        });
      };

      const handleSsePayload = async (payload: string) => {
        if (!payload.trim()) return;
        const data = JSON.parse(payload);

        if (data.type === 'thinking') {
          upsertThinking(data);
        } else if (data.type === 'thinking_done') {
          completeThinking();
        } else if (data.type === 'text') {
          assistantContent += data.content;
          updateAssistantMessage(assistantContent, thinkingForMessage);
        } else if (data.type === 'answer_reset') {
          assistantContent = '';
          updateAssistantMessage(assistantContent, thinkingForMessage);
        } else if (data.type === 'done') {
          finalizeAssistantMessage(
            typeof data.content === 'string' ? data.content : undefined
          );
        } else if (data.type === 'stopped') {
          upsertThinking({
            id: `stopped-${sessionId}`,
            kind: 'status',
            state: 'completed',
            message: data.message || '任务已停止'
          });
          finalizeAssistantMessage(assistantContent || '任务已停止。');
        } else if (data.type === 'error') {
          upsertThinking({
            id: `error-${++activitySequence}`,
            kind: 'status',
            state: 'error',
            message: data.message || '处理请求时发生错误'
          });
          throw new Error(data.message);
        }
      };

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          sseBuffer += decoder.decode(value, { stream: true });
          const rawEvents = sseBuffer.split('\n\n');
          sseBuffer = rawEvents.pop() || '';

          for (const eventChunk of rawEvents) {
            const dataLines = eventChunk
              .split('\n')
              .filter(line => line.startsWith('data: '))
              .map(line => line.substring(6));

            if (dataLines.length === 0) continue;

            try {
              await handleSsePayload(dataLines.join('\n'));
            } catch (e) {
              console.error('Error parsing SSE data:', e);
            }
          }
        }

        // Flush any trailing SSE payload still in the buffer
        const tail = sseBuffer.trim();
        if (tail.startsWith('data: ')) {
          try {
            await handleSsePayload(tail.substring(6));
          } catch (e) {
            console.error('Error parsing trailing SSE data:', e);
          }
        }

        if (!didFinalize) {
          finalizeAssistantMessage();
        }
      }
    } catch (error) {
      const wasAborted = error instanceof DOMException && error.name === 'AbortError';
      if (!wasAborted) console.error('Failed to send message:', error);
      const errMsg = error instanceof Error ? error.message : String(error);
      updateSessionMessages(sessionId, prev => {
        const newMessages = [...prev];
        const lastMsg = newMessages[newMessages.length - 1];
        if (lastMsg && lastMsg.role === 'assistant') {
          if (wasAborted) {
            lastMsg.content = lastMsg.content || '任务已停止。';
            lastMsg.thinkingCollapsed = true;
          } else if (lastMsg.content === '') {
            lastMsg.content = `请求失败：${errMsg}`;
          }
        }
        return newMessages;
      });
    } finally {
      if (abortControllersRef.current.get(sessionId) === abortController) {
        abortControllersRef.current.delete(sessionId);
      }
      setLoadingSessionIds(previous => {
        const next = new Set(previous);
        next.delete(sessionId);
        return next;
      });
    }
  };

  const stopCurrentTask = async () => {
    const sessionId = currentSessionId;
    if (!sessionId || !loadingSessionIds.has(sessionId)) return;

    const stopRequest = apiService.stopThread(sessionId).catch(error => {
      console.error('Failed to stop backend task:', error);
    });
    abortControllersRef.current.get(sessionId)?.abort();
    await stopRequest;
  };

  const handleExampleQuery = (query: string) => {
    setInputValue(query);
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessageStream();
    }
  };

  return (
    <div className="app">
      {/* Sidebar */}
      <div className={`sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-header">
          <h1 className="sidebar-title gradient-text">MAF Data Insight</h1>
          <button
            className="toggle-btn"
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          >
            {sidebarCollapsed ? '→' : '←'}
          </button>
        </div>

        <div className="sidebar-content">
          {/* Function Buttons Section */}
          <div className="nav-section">
            <div className="nav-section-title">Functions</div>
            <button className="nav-item" onClick={createNewSession}>
              <span className="nav-icon">➕</span>
              <span className="nav-text">New Session</span>
            </button>
          </div>

          {/* Current Chat Section */}
          <div className="nav-section">
            <div className="nav-section-title">Active Session</div>
            {!sidebarCollapsed && sessions.map((session) => (
              <button
                key={session.id}
                className={`nav-item ${session.id === currentSessionId ? 'active' : ''}`}
                onClick={() => switchSession(session.id)}
                style={{
                  fontSize: '13px',
                  padding: '10px 12px',
                  marginBottom: '4px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between'
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1 }}>
                  <span className="nav-icon">💬</span>
                  <span className="nav-text" style={{ fontSize: '13px' }}>
                    {session.name}{loadingSessionIds.has(session.id) ? ' · Running' : ''}
                  </span>
                </div>
                <span
                  className="delete-session-btn"
                  onClick={(e) => deleteSession(session.id, e)}
                  style={{
                    fontSize: '16px',
                    opacity: 0.6,
                    cursor: 'pointer',
                    padding: '0 4px',
                    transition: 'opacity 0.2s'
                  }}
                  onMouseEnter={(e) => e.currentTarget.style.opacity = '1'}
                  onMouseLeave={(e) => e.currentTarget.style.opacity = '0.6'}
                  title="删除会话"
                >
                  🗑️
                </span>
              </button>
            ))}
          </div>

          {!sidebarCollapsed && (
            <div className="nav-section">
              <div className="nav-section-title">示例问题</div>
              {EXAMPLE_QUERIES.map((query, idx) => (
                <button
                  key={idx}
                  className="nav-item example-query"
                  onClick={() => handleExampleQuery(query)}
                  style={{
                    fontSize: '11px',
                    padding: '8px 10px',
                    whiteSpace: 'normal',
                    textAlign: 'left',
                    lineHeight: '1.4',
                    wordWrap: 'break-word',
                    overflowWrap: 'break-word',
                    height: 'auto',
                    minHeight: '35px',
                    display: 'flex',
                    gap: '6px',
                    alignItems: 'flex-start'
                  }}
                >
                  <span style={{ flexShrink: 0, fontSize: '14px' }}>💡</span>
                  <span style={{ fontSize: '11px', flex: 1, wordBreak: 'break-word' }}>
                    {query}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="main-content">
        <div className="chat-header">
          <div className="chat-title">
            {currentSessionId ? `Session: ${currentSessionId.substring(0, 20)}...` : 'MAF Data Insight Agent'}
          </div>
        </div>

        {(
          <>
            <div className="chat-container" ref={chatContainerRef}>
              {messages.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">🤖</div>
                  <h2 className="empty-state-title">Welcome to MAF Data Insight Agent</h2>
                  <p className="empty-state-desc">
                    Ask me anything about enterprise knowledge, vehicle data analytics, or Databricks schema.
                    <br />
                    Try one of the example queries from the sidebar to get started!
                  </p>
                </div>
              ) : (
                <>
                  {messages.map((msg, idx) => (
                    <div key={idx}>
                      {/* User message */}
                      {msg.role === 'user' && (
                        <div className="message user">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {msg.content}
                          </ReactMarkdown>
                          <span className="message-timestamp">
                            {new Date(msg.timestamp).toLocaleTimeString()}
                          </span>
                        </div>
                      )}
                      
                      {/* Assistant message with thinking */}
                      {msg.role === 'assistant' && (
                        <>
                          {/* Thinking section - always present, collapsible */}
                          {msg.thinking && msg.thinking.length > 0 && (
                            <ActivityPanel
                              key={`activity-${idx}-${msg.thinkingCollapsed ? 'complete' : 'active'}`}
                              activities={msg.thinking}
                              complete={Boolean(msg.thinkingCollapsed)}
                            />
                          )}
                          
                          {/* Assistant response */}
                          <div className="message assistant">
                            <ReactMarkdown
                              remarkPlugins={[remarkGfm]}
                              urlTransform={(url) => url}
                              components={{
                                a: ({ href, node, children, ...props }) => {
                                  const childArr = React.Children.toArray(children);
                                  const onlyImg =
                                    childArr.length === 1 &&
                                    React.isValidElement(childArr[0]) &&
                                    (childArr[0] as React.ReactElement).type === 'img';
                                  if (onlyImg) return <>{children}</>;
                                  // Fragment links (#...) are same-page footnote anchors — keep default navigation
                                  const isFragment = href?.startsWith('#');
                                  return (
                                    <a
                                      {...props}
                                      href={href}
                                      {...(!isFragment ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
                                    >
                                      {children}
                                    </a>
                                  );
                                },
                                img: ({ src, alt }) => <ProxiedImage src={src} alt={alt} />
                              }}
                            >
                              {repairCollapsedMarkdownTables(msg.content)}
                            </ReactMarkdown>
                            <span className="message-timestamp">
                              {new Date(msg.timestamp).toLocaleTimeString()}
                            </span>
                          </div>
                        </>
                      )}
                    </div>
                  ))}
                  
                  {/* Loading indicator under thinking */}
                  {isLoading && (
                    <div className="loading">
                      <div className="spinner"></div>
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="input-container">
              <div className="input-wrapper">
                <textarea
                  className="chat-input"
                  value={inputValue}
                  onChange={(e) => setInputValue(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder="Ask about enterprise standards, data analytics, or Databricks schema..."
                  disabled={isLoading}
                />
                <button
                  className={`send-btn ${isLoading ? 'stop' : ''}`}
                  onClick={isLoading ? stopCurrentTask : sendMessageStream}
                  disabled={!isLoading && !inputValue.trim()}
                  title={isLoading ? '停止当前 Session 的任务' : '发送消息'}
                >
                  {isLoading ? '■ Stop' : 'Send'}
                </button>
              </div>
            </div>
        </>
        )}
      </div>
    </div>
  );
}

export default App;
