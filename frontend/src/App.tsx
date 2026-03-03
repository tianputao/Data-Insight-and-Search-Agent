import React, { useState, useEffect, useRef } from 'react';
import { apiService } from './services/api';
import type { ChatMessage, SessionInfo } from './types';
import './styles/global.css';
import './styles/App.css';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Example questions from the enterprise knowledge base (matching app.py EXAMPLE_QUESTIONS)
const EXAMPLE_QUERIES = [
  "汽车用液化天然气的加液口基本构型",
  "电动汽车用动力蓄电池安全要求",
  "What are the recall criteria for defective automotive products",
  "什么是management body, 它在乘用车法规里做什么用的，目前发行了几个版本",
  "哪个客户在2023年的消费是最高的",
  "按月看2022年的销售额趋势",
  "按产品类别看2023的销量"
];

interface ThinkingMessage {
  type: 'thinking';
  content: string;
  timestamp: string;
}

interface MessageWithThinking extends ChatMessage {
  thinking?: ThinkingMessage[];
  thinkingCollapsed?: boolean;
}

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
  for (const m of refsText.matchAll(/\[(\d+)\]\s*\[(.*?)\]\((https?:\/\/[^)]+)\)/g)) {
    refsMap.set(m[1], { title: (m[2] || '').trim() || `Reference ${m[1]}`, url: m[3].trim() });
  }

  for (const m of refsText.matchAll(/\[(\d+)\]\s+([^\n\[][^\n]*)/g)) {
    const num = m[1];
    if (!refsMap.has(num)) {
      refsMap.set(num, { title: (m[2] || '').trim() || `Reference ${num}`, url: '' });
    }
  }

  for (const m of body.matchAll(/\[\[(\d+)\]\]\((https?:\/\/[^)]+)\)/g)) {
    if (!refsMap.has(m[1])) {
      refsMap.set(m[1], { title: `Reference ${m[1]}`, url: m[2].trim() });
    }
  }

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

  let normalizedBody = body.replace(/\[\[(\d+)\]\]\((https?:\/\/[^)]+)\)/g, (_all, oldNum, url) => {
    const newNum = remap.get(oldNum) || oldNum;
    return `[[${newNum}]](${url})`;
  });
  normalizedBody = normalizedBody.replace(/\[\[(\d+)\]\]/g, (_all, oldNum) => {
    const newNum = remap.get(oldNum) || oldNum;
    return `[[${newNum}]]`;
  });

  const refLines = mergedOrder.map(oldNum => {
    const item = refsMap.get(oldNum);
    const newNum = remap.get(oldNum) || oldNum;
    if (!item) return '';
    if (item.url && /^https?:\/\//.test(item.url)) {
      return `[${newNum}] [${item.title}](${item.url})`;
    }
    return `[${newNum}] ${item.title}`;
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
  const [messages, setMessages] = useState<MessageWithThinking[]>([]);
  const [currentThinking, setCurrentThinking] = useState<ThinkingMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionCounter, setSessionCounter] = useState(1);
  const [autoScroll, setAutoScroll] = useState(true);
  const [sessionMessages, setSessionMessages] = useState<Map<string, MessageWithThinking[]>>(new Map());
  const chatContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Create the initial session on mount
    if (sessions.length === 0) {
      createInitialSession();
    }
  }, []);

  useEffect(() => {
    if (chatContainerRef.current && autoScroll) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
    }
  }, [messages, currentThinking, autoScroll]);

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

  const toggleThinking = (messageIndex: number) => {
    setMessages(prev => prev.map((msg, idx) => 
      idx === messageIndex 
        ? { ...msg, thinkingCollapsed: !msg.thinkingCollapsed }
        : msg
    ));
  };

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
      setMessages([]);
      setCurrentThinking([]);
    } catch (error) {
      console.error('Failed to create initial session:', error);
    }
  };

  const createNewSession = async () => {
    try {
      // 保存当前session的消息
      if (currentSessionId && messages.length > 0) {
        setSessionMessages(prev => new Map(prev).set(currentSessionId, messages));
      }

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
      setMessages([]);
      setCurrentThinking([]);
    } catch (error) {
      console.error('Failed to create session:', error);
    }
  };

  const switchSession = (sessionId: string) => {
    // 保存当前session的消息
    if (currentSessionId && messages.length > 0) {
      setSessionMessages(prev => new Map(prev).set(currentSessionId, messages));
    }

    // 加载目标session的消息
    const savedMessages = sessionMessages.get(sessionId) || [];
    setCurrentSessionId(sessionId);
    setMessages(savedMessages);
    setCurrentThinking([]);
  };

  const deleteSession = async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    
    // 如果只剩一个session，不允许删除
    if (sessions.length === 1) {
      alert('至少需要保留一个会话');
      return;
    }

    try {
      // 删除session的消息记录
      setSessionMessages(prev => {
        const newMap = new Map(prev);
        newMap.delete(sessionId);
        return newMap;
      });

      setSessions(prev => prev.filter(s => s.id !== sessionId));
      
      // 如果删除的是当前session，切换到第一个session
      if (currentSessionId === sessionId) {
        const remainingSessions = sessions.filter(s => s.id !== sessionId);
        if (remainingSessions.length > 0) {
          switchSession(remainingSessions[0].id);
        }
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
    }
  };

  const sendMessageStream = async () => {
    if (!inputValue.trim() || isLoading) return;

    const userMessage: MessageWithThinking = {
      role: 'user',
      content: inputValue,
      timestamp: new Date().toISOString()
    };

    setMessages(prev => [...prev, userMessage]);
    const currentInput = inputValue;
    setInputValue('');
    setIsLoading(true);
    setCurrentThinking([]);
    setAutoScroll(true); // 开始新消息时启用自动滚动

    // 先创建一个空的assistant消息框架，thinking在前面
    const initialAssistantMessage: MessageWithThinking = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      thinking: [],
      thinkingCollapsed: false
    };
    setMessages(prev => [...prev, initialAssistantMessage]);

    try {
      const response = await fetch(apiUrl('/chat/stream'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          message: currentInput,
          thread_id: currentSessionId
        })
      });

      if (!response.ok) {
        const errBody = await response.text().catch(() => '');
        throw new Error(`Failed to send message (${response.status}): ${errBody || response.statusText}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let assistantContent = '';
      let thinkingForMessage: ThinkingMessage[] = [];
      let sseBuffer = '';
      let pendingThinkingQueue: ThinkingMessage[] = [];
      let thinkingFlushTask: Promise<void> | null = null;

      const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

      const updateAssistantMessage = (content: string, thinking: ThinkingMessage[]) => {
        setMessages(prev => {
          const newMessages = [...prev];
          const lastMessage = newMessages[newMessages.length - 1];
          if (lastMessage && lastMessage.role === 'assistant') {
            lastMessage.content = content;
            lastMessage.thinking = [...thinking];
          }
          return newMessages;
        });
      };

      const enqueueThinking = (message: string) => {
        const currentLast = pendingThinkingQueue[pendingThinkingQueue.length - 1]?.content
          || thinkingForMessage[thinkingForMessage.length - 1]?.content;
        if (currentLast === message) {
          return;
        }

        pendingThinkingQueue.push({
          type: 'thinking',
          content: message,
          timestamp: new Date().toISOString()
        });

        if (!thinkingFlushTask) {
          thinkingFlushTask = (async () => {
            while (pendingThinkingQueue.length > 0) {
              const next = pendingThinkingQueue.shift();
              if (!next) break;
              thinkingForMessage.push(next);
              updateAssistantMessage(assistantContent, thinkingForMessage);
              await sleep(280);
            }
            thinkingFlushTask = null;
          })();
        }
      };

      const handleSsePayload = async (payload: string) => {
        if (!payload.trim()) return;
        const data = JSON.parse(payload);

        if (data.type === 'thinking') {
          enqueueThinking(data.message);
        } else if (data.type === 'text') {
          assistantContent += data.content;
          updateAssistantMessage(assistantContent, thinkingForMessage);
        } else if (data.type === 'done') {
          if (thinkingFlushTask) {
            await thinkingFlushTask;
          }

          assistantContent = normalizeCitationsForDisplay(assistantContent);
          updateAssistantMessage(assistantContent, thinkingForMessage);

          setTimeout(() => {
            setMessages(prev => {
              const newMessages = [...prev];
              const lastMsg = newMessages[newMessages.length - 1];
              if (lastMsg && lastMsg.role === 'assistant') {
                lastMsg.thinkingCollapsed = true;
              }
              return newMessages;
            });
          }, 2000);
        } else if (data.type === 'error') {
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
      }
    } catch (error) {
      console.error('Failed to send message:', error);
      const errMsg = error instanceof Error ? error.message : String(error);
      setMessages(prev => {
        const newMessages = [...prev];
        const lastMsg = newMessages[newMessages.length - 1];
        if (lastMsg && lastMsg.role === 'assistant' && lastMsg.content === '') {
          lastMsg.content = `请求失败：${errMsg}`;
        }
        return newMessages;
      });
    } finally {
      setIsLoading(false);
    }
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
                    {session.name}
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
                            <div className="thinking-container">
                              <button 
                                className="thinking-toggle"
                                onClick={() => toggleThinking(idx)}
                              >
                                <span className="thinking-icon">
                                  {msg.thinkingCollapsed ? '▶' : '▼'}
                                </span>
                                <span className="thinking-title">
                                  思考过程 ({msg.thinking.length} steps)
                                </span>
                              </button>
                              {!msg.thinkingCollapsed && (
                                <div className="thinking-content">
                                  {msg.thinking.map((thinking, tIdx) => (
                                    <div key={tIdx} className="thinking-step">
                                      {thinking.content}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
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
                              {msg.content}
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
                  className="send-btn"
                  onClick={sendMessageStream}
                  disabled={isLoading || !inputValue.trim()}
                >
                  {isLoading ? 'Sending...' : 'Send 🚀'}
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
