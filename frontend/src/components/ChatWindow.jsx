import React, { useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAuth } from '../context/AuthContext'

function TypingIndicator() {
  return (
    <div className="flex items-end gap-2 mb-4">
      <div className="w-7 h-7 rounded-full bg-brand-500/20 border border-brand-500/30 flex items-center justify-center flex-shrink-0 text-sm">🤖</div>
      <div className="bg-slate-800 border border-slate-700 rounded-2xl rounded-bl-sm px-4 py-3">
        <div className="flex gap-1 items-center h-4">
          <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
          <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
          <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
      </div>
    </div>
  )
}

function Message({ msg }) {
  const isUser = msg.role === 'user'

  const formatTime = iso => {
    if (!iso) return ''
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  if (isUser) {
    return (
      <div className="flex justify-end mb-4">
        <div className="max-w-[75%]">
          <div className="bg-brand-500 text-white rounded-2xl rounded-br-sm px-4 py-3 text-sm leading-relaxed">
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
    <div className="flex items-end gap-2 mb-4">
      <div className="w-7 h-7 rounded-full bg-brand-500/20 border border-brand-500/30 flex items-center justify-center flex-shrink-0 text-sm">🤖</div>
      <div className="max-w-[75%]">
        <div className="bg-slate-800 border border-slate-700 text-slate-100 rounded-2xl rounded-bl-sm px-4 py-3 text-sm leading-relaxed prose-chat">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
        </div>
        {msg.timestamp && (
          <p className="text-xs text-slate-500 mt-1">{formatTime(msg.timestamp)}</p>
        )}
      </div>
    </div>
  )
}

export default function ChatWindow({ messages, isTyping, threadTitle }) {
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
            <div className="text-5xl mb-4">🤖</div>
            <h3 className="text-white font-semibold text-lg mb-2">How can I help you today?</h3>
            <p className="text-slate-400 text-sm max-w-sm">
              Ask me anything — employee info, send emails, manage calendar events, or just chat.
            </p>
          </div>
        ) : (
          <>
            {messages.map((msg, i) => <Message key={i} msg={msg} />)}
            {isTyping && <TypingIndicator />}
          </>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
