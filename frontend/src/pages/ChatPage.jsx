import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import api from '../api/axios'
import Sidebar      from '../components/Sidebar'
import ChatWindow   from '../components/ChatWindow'
import MessageInput from '../components/MessageInput'
import ProfileModal from '../components/ProfileModal'

export default function ChatPage() {
  const { logout }   = useAuth()
  const navigate     = useNavigate()

  const [threads, setThreads]             = useState([])
  const [activeThreadId, setActiveThreadId] = useState(null)
  const [activeThreadTitle, setActiveThreadTitle] = useState('')
  const [messages, setMessages]           = useState([])
  const [isTyping, setIsTyping]           = useState(false)
  const [loadingThreads, setLoadingThreads] = useState(true)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [showProfile, setShowProfile]     = useState(false)
  const [sidebarOpen, setSidebarOpen]     = useState(true)

  // ── Load thread list ────────────────────────────────────────────────────────
  const fetchThreads = useCallback(async () => {
    try {
      const res = await api.get('/chat/threads')
      const list = res.data.threads || []
      setThreads(list)
      // Set active thread from list
      const active = list.find(t => t.active)
      if (active) {
        setActiveThreadId(active.thread_id)
        setActiveThreadTitle(active.title || 'New Conversation')
      }
      return list
    } catch {
      // silently fail
    } finally {
      setLoadingThreads(false)
    }
  }, [])

  // ── Load messages for a thread ──────────────────────────────────────────────
  const fetchMessages = useCallback(async (threadId) => {
    if (!threadId) return
    setLoadingMessages(true)
    try {
      const res = await api.get(`/chat/threads/${threadId}/messages`, { params: { limit: 50 } })
      setMessages(res.data.messages || [])
    } catch {
      setMessages([])
    } finally {
      setLoadingMessages(false)
    }
  }, [])

  // On mount — load threads then load messages for active thread
  useEffect(() => {
    fetchThreads().then(list => {
      if (!list) return
      const active = list.find(t => t.active)
      if (active) fetchMessages(active.thread_id)
    })
  }, [fetchThreads, fetchMessages])

  // ── New chat ────────────────────────────────────────────────────────────────
  const handleNewChat = async () => {
    try {
      const res = await api.post('/chat/session/new')
      const newId = res.data.thread_id
      setActiveThreadId(newId)
      setActiveThreadTitle('New Conversation')
      setMessages([])
      await fetchThreads()
    } catch (err) {
      console.error('Failed to create new chat', err)
    }
  }

  // ── Select / switch thread ──────────────────────────────────────────────────
  const handleSelectThread = async (threadId) => {
    if (threadId === activeThreadId) return
    try {
      await api.post(`/chat/threads/${threadId}/switch`)
      const thread = threads.find(t => t.thread_id === threadId)
      setActiveThreadId(threadId)
      setActiveThreadTitle(thread?.title || 'New Conversation')
      await fetchMessages(threadId)
      await fetchThreads()
    } catch (err) {
      console.error('Failed to switch thread', err)
    }
  }

  // ── Delete thread ───────────────────────────────────────────────────────────
  const handleDeleteThread = async (threadId) => {
    try {
      const res = await api.delete(`/chat/threads/${threadId}`)
      if (res.data.new_thread_id) {
        // Active thread was deleted — switch to new one
        setActiveThreadId(res.data.new_thread_id)
        setActiveThreadTitle('New Conversation')
        setMessages([])
      } else if (threadId === activeThreadId) {
        setMessages([])
        setActiveThreadId(null)
        setActiveThreadTitle('')
      }
      await fetchThreads()
    } catch (err) {
      console.error('Failed to delete thread', err)
    }
  }

  // ── Send message ────────────────────────────────────────────────────────────
  const handleSend = async (text) => {
    // Optimistically add user message
    const userMsg = { role: 'user', content: text, timestamp: new Date().toISOString() }
    setMessages(prev => [...prev, userMsg])
    setIsTyping(true)

    try {
      const res = await api.post('/chat/', { message: text })
      const assistantMsg = {
        role: 'assistant',
        content: res.data.response,
        timestamp: new Date().toISOString(),
      }
      setMessages(prev => [...prev, assistantMsg])

      // Refresh thread list (title may have been generated after first turn)
      await fetchThreads()
      // Update title in header
      const updated = await api.get('/chat/threads')
      const active = (updated.data.threads || []).find(t => t.thread_id === activeThreadId)
      if (active?.title) setActiveThreadTitle(active.title)

    } catch (err) {
      const errMsg = {
        role: 'assistant',
        content: err.response?.data?.detail || 'Something went wrong. Please try again.',
        timestamp: new Date().toISOString(),
        isError: true,
      }
      setMessages(prev => [...prev, errMsg])
    } finally {
      setIsTyping(false)
    }
  }

  // ── Logout ──────────────────────────────────────────────────────────────────
  const handleLogout = async () => {
    try { await api.post('/chat/session/end') } catch { /* best effort */ }
    await logout()
    navigate('/login')
  }

  return (
    <div className="h-screen bg-slate-950 flex overflow-hidden">
      {/* Mobile sidebar toggle */}
      <button
        onClick={() => setSidebarOpen(o => !o)}
        className="md:hidden fixed top-3 left-3 z-40 p-2 rounded-xl bg-slate-800 text-slate-300 hover:text-white transition"
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

      {/* Sidebar */}
      <div className={`
        fixed md:relative inset-y-0 left-0 z-30 transition-transform duration-300
        ${sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
      `}>
        <Sidebar
          threads={threads}
          activeThreadId={activeThreadId}
          onNewChat={handleNewChat}
          onSelectThread={handleSelectThread}
          onDeleteThread={handleDeleteThread}
          onOpenProfile={() => setShowProfile(true)}
          onLogout={handleLogout}
          loadingThreads={loadingThreads}
        />
      </div>

      {/* Mobile backdrop */}
      {sidebarOpen && (
        <div
          className="md:hidden fixed inset-0 bg-black/50 z-20"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Main chat area */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {loadingMessages ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : (
          <ChatWindow
            messages={messages}
            isTyping={isTyping}
            threadTitle={activeThreadTitle}
          />
        )}
        <MessageInput onSend={handleSend} disabled={isTyping} />
      </main>

      {/* Profile modal */}
      {showProfile && <ProfileModal onClose={() => setShowProfile(false)} />}
    </div>
  )
}
