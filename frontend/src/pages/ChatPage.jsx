// import React, { useState, useEffect, useCallback } from 'react'
// import { useNavigate } from 'react-router-dom'
// import { useAuth } from '../context/AuthContext'
// import api from '../api/axios'
// import Sidebar      from '../components/Sidebar'
// import ChatWindow   from '../components/ChatWindow'
// import MessageInput from '../components/MessageInput'
// import ProfileModal from '../components/ProfileModal'
// import GoogleConnect from '../components/GoogleConnect'

// export default function ChatPage() {
//   const { logout }   = useAuth()
//   const navigate     = useNavigate()

//   const [threads, setThreads]             = useState([])
//   const [activeThreadId, setActiveThreadId] = useState(null)
//   const [activeThreadTitle, setActiveThreadTitle] = useState('')
//   const [messages, setMessages]           = useState([])
//   const [isTyping, setIsTyping]           = useState(false)
//   const [loadingThreads, setLoadingThreads] = useState(true)
//   const [loadingMessages, setLoadingMessages] = useState(false)
//   const [showProfile, setShowProfile]     = useState(false)
//   const [sidebarOpen, setSidebarOpen]     = useState(true)

//   // ── Load thread list ────────────────────────────────────────────────────────
//   // NOTE: fetchThreads only updates the sidebar list — it never overwrites
//   // activeThreadId so local state (set by handleNewChat / handleSelectThread)
//   // always wins over whatever the server thinks is "active".
//   const fetchThreads = useCallback(async () => {
//     try {
//       const res = await api.get('/chat/threads')
//       const list = res.data.threads || []
//       setThreads(list)
//       return list
//     } catch {
//       // silently fail
//     } finally {
//       setLoadingThreads(false)
//     }
//   }, [])

//   // ── Load messages for a thread ──────────────────────────────────────────────
//   const fetchMessages = useCallback(async (threadId) => {
//     if (!threadId) return
//     setLoadingMessages(true)
//     try {
//       const res = await api.get(`/chat/threads/${threadId}/messages`, { params: { limit: 50 } })
//       setMessages(res.data.messages || [])
//     } catch {
//       setMessages([])
//     } finally {
//       setLoadingMessages(false)
//     }
//   }, [])

//   // On mount — load threads then load messages for the server-active thread
//   useEffect(() => {
//     fetchThreads().then(list => {
//       if (!list) return
//       const active = list.find(t => t.active)
//       if (active) {
//         setActiveThreadId(active.thread_id)
//         setActiveThreadTitle(active.title || 'New Conversation')
//         fetchMessages(active.thread_id)
//       }
//     })
//   }, []) // eslint-disable-line react-hooks/exhaustive-deps

//   // ── Flush summary when tab is closed or hidden ───────────────────────────────
//   useEffect(() => {
//     const handleVisibility = () => {
//       if (document.visibilityState === 'hidden') {
//         const token = localStorage.getItem('access_token')
//         if (token) {
//           navigator.sendBeacon(
//             'http://127.0.0.1:8000/chat/session/end',
//             new Blob([JSON.stringify({})], { type: 'application/json' })
//           )
//         }
//       }
//     }
//     document.addEventListener('visibilitychange', handleVisibility)
//     return () => document.removeEventListener('visibilitychange', handleVisibility)
//   }, [])

//   // ── New chat ────────────────────────────────────────────────────────────────
//   // Bug 3 fix: block creating a new thread if the current active thread is
//   // already empty (title is still 'New Conversation' and no messages)
//   const handleNewChat = async () => {
//     const currentIsEmpty = messages.length === 0
//     if (currentIsEmpty) return   // already on a blank thread, do nothing
//     try {
//       const res = await api.post('/chat/session/new')
//       const newId = res.data.thread_id
//       setActiveThreadId(newId)
//       setActiveThreadTitle('New Conversation')
//       setMessages([])
//       await fetchThreads()
//     } catch (err) {
//       console.error('Failed to create new chat', err)
//     }
//   }

//   // ── Select / switch thread ──────────────────────────────────────────────────
//   const handleSelectThread = async (threadId) => {
//     if (threadId === activeThreadId) return
//     try {
//       await api.post(`/chat/threads/${threadId}/switch`)
//       const thread = threads.find(t => t.thread_id === threadId)
//       setActiveThreadId(threadId)
//       setActiveThreadTitle(thread?.title || 'New Conversation')
//       await fetchMessages(threadId)
//       await fetchThreads()
//     } catch (err) {
//       console.error('Failed to switch thread', err)
//     }
//   }

//   // ── Delete thread ───────────────────────────────────────────────────────────
//   const handleDeleteThread = async (threadId) => {
//     try {
//       const res = await api.delete(`/chat/threads/${threadId}`)
//       if (res.data.new_thread_id) {
//         // Active thread was deleted — switch to new one
//         setActiveThreadId(res.data.new_thread_id)
//         setActiveThreadTitle('New Conversation')
//         setMessages([])
//       } else if (threadId === activeThreadId) {
//         setMessages([])
//         setActiveThreadId(null)
//         setActiveThreadTitle('')
//       }
//       await fetchThreads()
//     } catch (err) {
//       console.error('Failed to delete thread', err)
//     }
//   }

//   // ── Send message ────────────────────────────────────────────────────────────
//   const handleSend = async (text) => {
//     // Capture activeThreadId at the moment of send — closure-safe
//     const threadIdAtSend = activeThreadId

//     const userMsg = { role: 'user', content: text, timestamp: new Date().toISOString() }
//     setMessages(prev => [...prev, userMsg])
//     setIsTyping(true)

//     try {
//       const res = await api.post('/chat/', { message: text })
//       const assistantMsg = {
//         role: 'assistant',
//         content: res.data.response,
//         timestamp: new Date().toISOString(),
//       }
//       setMessages(prev => [...prev, assistantMsg])

//       // Refresh sidebar immediately
//       const list = await fetchThreads()
//       if (list) {
//         const sent = list.find(t => t.thread_id === threadIdAtSend)
//         if (sent?.title && sent.title !== 'New Conversation') {
//           setActiveThreadTitle(sent.title)
//         } else {
//           // Title is generated async on backend after first turn — poll once after 3s
//           setTimeout(async () => {
//             const delayed = await fetchThreads()
//             if (delayed) {
//               const t = delayed.find(t => t.thread_id === threadIdAtSend)
//               if (t?.title && t.title !== 'New Conversation') {
//                 setActiveThreadTitle(t.title)
//               }
//             }
//           }, 3000)
//         }
//       }
//     } catch (err) {
//       const errMsg = {
//         role: 'assistant',
//         content: err.response?.data?.detail || 'Something went wrong. Please try again.',
//         timestamp: new Date().toISOString(),
//         isError: true,
//         retryText: text,
//       }
//       setMessages(prev => [...prev, errMsg])
//     } finally {
//       setIsTyping(false)
//     }
//   }

//   // ── Retry failed message ───────────────────────────────────────────────────────────
//   const handleRetry = (text) => {
//     if (!text) return
//     // Remove the last error message then resend
//     setMessages(prev => prev.filter(m => !m.isError))
//     handleSend(text)
//   }

//   // ── Logout ──────────────────────────────────────────────────────────────────
//   const handleLogout = async () => {
//     try { await api.post('/chat/session/end') } catch { /* best effort */ }
//     await logout()
//     navigate('/login')
//   }

//   return (
//     <div className="h-screen bg-slate-950 flex overflow-hidden">
//       {/* Mobile sidebar toggle */}
//       <button
//         onClick={() => setSidebarOpen(o => !o)}
//         className="md:hidden fixed top-3 left-3 z-40 p-2 rounded-xl bg-slate-800 text-slate-300 hover:text-white transition"
//       >
//         <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
//           <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
//         </svg>
//       </button>

//       {/* Sidebar */}
//       <div className={`
//         fixed md:relative inset-y-0 left-0 z-30 transition-transform duration-300
//         ${sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
//       `}>
//         <Sidebar
//           threads={threads}
//           activeThreadId={activeThreadId}
//           onNewChat={handleNewChat}
//           onSelectThread={handleSelectThread}
//           onDeleteThread={handleDeleteThread}
//           onOpenProfile={() => setShowProfile(true)}
//           onLogout={handleLogout}
//           loadingThreads={loadingThreads}
//           isCurrentThreadEmpty={messages.length === 0}
//         />
//       </div>

//       {/* Mobile backdrop */}
//       {sidebarOpen && (
//         <div
//           className="md:hidden fixed inset-0 bg-black/50 z-20"
//           onClick={() => setSidebarOpen(false)}
//         />
//       )}

//       {/* Main chat area */}
//       <main className="flex-1 flex flex-col overflow-hidden">
//         <GoogleConnect />
//         {loadingMessages ? (
//           <div className="flex-1 flex items-center justify-center">
//             <div className="w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin" />
//           </div>
//         ) : (
//           <ChatWindow
//             messages={messages}
//             isTyping={isTyping}
//             threadTitle={activeThreadTitle}
//             onRetry={handleRetry}
//           />
//         )}
//         <MessageInput onSend={handleSend} disabled={isTyping} />
//       </main>

//       {/* Profile modal */}
//       {showProfile && <ProfileModal onClose={() => setShowProfile(false)} />}
//     </div>
//   )
// }




import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import api from '../api/axios'
import Sidebar      from '../components/Sidebar'
import ChatWindow   from '../components/ChatWindow'
import MessageInput from '../components/MessageInput'
import ProfileModal from '../components/ProfileModal'
import GoogleConnect from '../components/GoogleConnect'

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

  // ── Attachment state ────────────────────────────────────────────────────────
  // Each entry: { filename: string, size: number }
  const [attachments, setAttachments] = useState([])

  // ── Load thread list ────────────────────────────────────────────────────────
  const fetchThreads = useCallback(async () => {
    try {
      const res = await api.get('/chat/threads')
      const list = res.data.threads || []
      setThreads(list)
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

  // On mount — load threads then load messages for the server-active thread
  useEffect(() => {
    fetchThreads().then(list => {
      if (!list) return
      const active = list.find(t => t.active)
      if (active) {
        setActiveThreadId(active.thread_id)
        setActiveThreadTitle(active.title || 'New Conversation')
        fetchMessages(active.thread_id)
      }
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Flush summary when tab is closed or hidden ───────────────────────────────
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        const token = localStorage.getItem('access_token')
        if (token) {
          navigator.sendBeacon(
            'http://127.0.0.1:8000/chat/session/end',
            new Blob([JSON.stringify({})], { type: 'application/json' })
          )
        }
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [])

  // ── New chat ────────────────────────────────────────────────────────────────
  const handleNewChat = async () => {
    const currentIsEmpty = messages.length === 0
    if (currentIsEmpty) return
    try {
      const res = await api.post('/chat/session/new')
      const newId = res.data.thread_id
      setActiveThreadId(newId)
      setActiveThreadTitle('New Conversation')
      setMessages([])
      setAttachments([])
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
      setAttachments([])   // clear attachments when switching threads
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
        setActiveThreadId(res.data.new_thread_id)
        setActiveThreadTitle('New Conversation')
        setMessages([])
        setAttachments([])
      } else if (threadId === activeThreadId) {
        setMessages([])
        setAttachments([])
        setActiveThreadId(null)
        setActiveThreadTitle('')
      }
      await fetchThreads()
    } catch (err) {
      console.error('Failed to delete thread', err)
    }
  }

  // ── Upload attachment ───────────────────────────────────────────────────────
  const handleUploadAttachment = async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    try {
      const res = await api.post('/chat/upload-attachment', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      // Add to local badge list (deduplicate by filename — last wins)
      setAttachments(prev => {
        const filtered = prev.filter(a => a.filename !== file.name)
        return [...filtered, { filename: res.data.filename, size: res.data.size }]
      })
    } catch (err) {
      console.error('Failed to upload attachment', err)
      const detail = err.response?.data?.detail || 'Upload failed. Please try again.'
      alert(detail)
    }
  }

  // ── Remove attachment ───────────────────────────────────────────────────────
  const handleRemoveAttachment = async (filename) => {
    try {
      await api.delete('/chat/upload-attachment', { params: { filename } })
      setAttachments(prev => prev.filter(a => a.filename !== filename))
    } catch (err) {
      console.error('Failed to remove attachment', err)
    }
  }

  // ── Send message ────────────────────────────────────────────────────────────
  const handleSend = async (text) => {
    const threadIdAtSend = activeThreadId

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

      // Clear attachment badges after a message is sent (the send endpoint consumes them)
      setAttachments([])

      const list = await fetchThreads()
      if (list) {
        const sent = list.find(t => t.thread_id === threadIdAtSend)
        if (sent?.title && sent.title !== 'New Conversation') {
          setActiveThreadTitle(sent.title)
        } else {
          setTimeout(async () => {
            const delayed = await fetchThreads()
            if (delayed) {
              const t = delayed.find(t => t.thread_id === threadIdAtSend)
              if (t?.title && t.title !== 'New Conversation') {
                setActiveThreadTitle(t.title)
              }
            }
          }, 3000)
        }
      }
    } catch (err) {
      const errMsg = {
        role: 'assistant',
        content: err.response?.data?.detail || 'Something went wrong. Please try again.',
        timestamp: new Date().toISOString(),
        isError: true,
        retryText: text,
      }
      setMessages(prev => [...prev, errMsg])
    } finally {
      setIsTyping(false)
    }
  }

  // ── Retry failed message ────────────────────────────────────────────────────
  const handleRetry = (text) => {
    if (!text) return
    setMessages(prev => prev.filter(m => !m.isError))
    handleSend(text)
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
          isCurrentThreadEmpty={messages.length === 0}
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
        <GoogleConnect />
        {loadingMessages ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : (
          <ChatWindow
            messages={messages}
            isTyping={isTyping}
            threadTitle={activeThreadTitle}
            onRetry={handleRetry}
          />
        )}
        <MessageInput
          onSend={handleSend}
          disabled={isTyping}
          attachments={attachments}
          onUploadAttachment={handleUploadAttachment}
          onRemoveAttachment={handleRemoveAttachment}
        />
      </main>

      {/* Profile modal */}
      {showProfile && <ProfileModal onClose={() => setShowProfile(false)} />}
    </div>
  )
}