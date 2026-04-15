import React, { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function TypingIndicator() {
  return (
    <div className="flex items-end gap-2 mb-4">
      <div className="w-7 h-7 rounded-full bg-brand-500/20 border border-brand-500/30 flex items-center justify-center flex-shrink-0 text-xs font-bold text-accent-400">AI</div>
      <div className="bg-slate-800 border border-slate-700 rounded-2xl rounded-bl-sm px-4 py-3">
        <div className="flex gap-1 items-center h-4">
          <span className="w-2 h-2 bg-accent-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
          <span className="w-2 h-2 bg-accent-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
          <span className="w-2 h-2 bg-accent-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
      </div>
    </div>
  )
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // fallback for older browsers
      const el = document.createElement('textarea')
      el.value = text
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <button
      onClick={handleCopy}
      title={copied ? 'Copied!' : 'Copy response'}
      className="opacity-0 group-hover:opacity-100 transition-opacity p-1.5 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-slate-700"
    >
      {copied ? (
        <svg className="w-3.5 h-3.5 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
      ) : (
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
        </svg>
      )}
    </button>
  )
}

// Normalize backend text: convert literal \n to real newlines
function normalizeContent(text) {
  if (!text) return ''
  return text
    .replace(/\\n/g, '\n')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
}

function Message({ msg, onRetry }) {
  const isUser = msg.role === 'user'

  const formatTime = iso => {
    if (!iso) return ''
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  if (isUser) {
    return (
      <div className="flex justify-end mb-4">
        <div className="max-w-[75%]">
          <div className="bg-brand-500 text-white rounded-2xl rounded-br-sm px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap">
            {msg.content}
          </div>
          {msg.timestamp && (
            <p className="text-xs text-slate-500 mt-1 text-right">{formatTime(msg.timestamp)}</p>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-start gap-2 mb-4 group">
      <div className="w-7 h-7 rounded-full bg-brand-500/20 border border-accent-400/40 flex items-center justify-center flex-shrink-0 text-xs font-bold text-accent-400 mt-1">AI</div>
      <div className="max-w-[75%]">
        <div className={`border text-slate-100 rounded-2xl rounded-tl-sm px-4 py-3 text-sm leading-relaxed prose-chat ${
          msg.isError
            ? 'bg-red-500/10 border-red-500/30 text-red-300'
            : 'bg-slate-800 border-slate-700'
        }`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{normalizeContent(msg.content)}</ReactMarkdown>
        </div>
        <div className="flex items-center gap-1 mt-1">
          {msg.timestamp && (
            <p className="text-xs text-slate-500">{formatTime(msg.timestamp)}</p>
          )}
          {!msg.isError && <CopyButton text={msg.content} />}
          {msg.isError && onRetry && (
            <button
              onClick={() => onRetry(msg.retryText)}
              className="flex items-center gap-1 text-xs text-red-400 hover:text-red-300 transition px-2 py-0.5 rounded-lg hover:bg-red-500/10"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              Retry
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function ChatWindow({ messages, isTyping, threadTitle, onRetry }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isTyping])

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Thread title bar */}
      <div className="px-6 py-3 border-b border-slate-800 bg-slate-900/50 backdrop-blur-sm flex-shrink-0">
        <h2 className="text-sm font-medium text-slate-300 truncate">
          {threadTitle || 'New Conversation'}
        </h2>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        {messages.length === 0 && !isTyping ? (
          <div className="h-full flex flex-col items-center justify-center text-center">
            <div className="w-12 h-12 rounded-2xl bg-brand-500/20 border border-brand-500/30 flex items-center justify-center mb-5">
              <span className="text-brand-500 font-bold text-sm">AI</span>
            </div>
            <h3 className="text-white font-bold text-2xl mb-2" style={{ fontFamily: "'Plus Jakarta Sans', sans-serif" }}>How can I help you today?</h3>
            <p className="text-slate-400 text-sm max-w-sm leading-relaxed">
              Ask me anything — employee info, send emails, manage calendar events, or just chat.
            </p>
          </div>
        ) : (
          <>
            {messages.map((msg, i) => <Message key={i} msg={msg} onRetry={onRetry} />)}
            {isTyping && <TypingIndicator />}
          </>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
