import React, { useState } from 'react'
import { useAuth } from '../context/AuthContext'

function ThreadItem({ thread, active, onSelect, onDelete }) {
  const [hovering, setHovering] = useState(false)

  const formatDate = iso => {
    if (!iso) return ''
    const d        = new Date(iso)
    const now      = new Date()
    const diffDays = Math.floor((now - d) / 86400000)
    if (diffDays === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (diffDays === 1) return 'Yesterday'
    if (diffDays < 7)  return d.toLocaleDateString([], { weekday: 'short' })
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }

  return (
    <div
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      onClick={() => onSelect(thread.thread_id)}
      className={`group relative flex items-start gap-2 px-3 py-2.5 rounded-xl cursor-pointer transition-all ${
        active
          ? 'bg-brand-500/20 border border-brand-500/30'
          : 'hover:bg-slate-800 border border-transparent'
      }`}
    >
      <div className="flex-1 min-w-0">
        <p className={`text-sm font-medium truncate ${active ? 'text-white' : 'text-slate-300'}`}>
          {thread.title || 'New Conversation'}
        </p>
        <p className="text-xs text-slate-500 mt-0.5">{formatDate(thread.updated_at)}</p>
      </div>

      {hovering && (
        <button
          onClick={e => { e.stopPropagation(); onDelete(thread.thread_id) }}
          className="flex-shrink-0 p-1 rounded-lg text-slate-500 hover:text-red-400 hover:bg-red-400/10 transition"
          title="Delete thread"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
        </button>
      )}
    </div>
  )
}

export default function Sidebar({
  threads,
  activeThreadId,
  onNewChat,
  onSelectThread,
  onDeleteThread,
  onOpenProfile,
  onLogout,
  loadingThreads,
  isCurrentThreadEmpty,
}) {
  const { user } = useAuth()

  return (
    <aside className="w-64 flex-shrink-0 bg-slate-900 border-r border-slate-800 flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-slate-800">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-8 h-8 rounded-lg bg-brand-500 flex items-center justify-center">
            <span className="text-white font-bold text-xs" style={{ fontFamily: "'Plus Jakarta Sans', sans-serif" }}>AI</span>
          </div>
          <span className="text-white font-bold text-sm" style={{ fontFamily: "'Plus Jakarta Sans', sans-serif" }}>AI Assistant</span>
        </div>
        <button
          onClick={onNewChat}
          disabled={isCurrentThreadEmpty}
          title={isCurrentThreadEmpty ? 'Current chat is already empty' : 'Start a new chat'}
          className="w-full flex items-center gap-2 bg-brand-500 hover:bg-brand-600 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-xl px-3 py-2.5 transition"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          New Chat
        </button>
      </div>

      {/* Thread list */}
      <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
        {loadingThreads ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-brand-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : threads.length === 0 ? (
          <p className="text-slate-500 text-xs text-center py-8 px-4">No conversations yet. Start a new chat!</p>
        ) : (
          threads.map(t => (
            <ThreadItem
              key={t.thread_id}
              thread={t}
              active={t.thread_id === activeThreadId}
              onSelect={onSelectThread}
              onDelete={onDeleteThread}
            />
          ))
        )}
      </div>

      {/* User footer */}
      <div className="p-3 border-t border-slate-800">
        <div className="flex items-center gap-2">
          {/* Avatar */}
          <div className="w-8 h-8 rounded-full bg-brand-500/20 border border-brand-500/30 flex items-center justify-center flex-shrink-0">
            <span className="text-brand-500 text-sm font-bold">
              {user?.display_name?.[0]?.toUpperCase() || '?'}
            </span>
          </div>

          <div className="flex-1 min-w-0">
            <p className="text-white text-xs font-medium truncate">{user?.display_name}</p>
            <p className="text-slate-500 text-xs truncate">@{user?.username}</p>
          </div>

          {/* Profile button */}
          <button
            onClick={onOpenProfile}
            title="Profile settings"
            className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-700 transition"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>

          {/* Logout button */}
          <button
            onClick={onLogout}
            title="Logout"
            className="p-1.5 rounded-lg text-slate-400 hover:text-red-400 hover:bg-red-400/10 transition"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
            </svg>
          </button>
        </div>
      </div>
    </aside>
  )
}
