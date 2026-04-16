import React, { useState, useEffect } from 'react'
import api from '../api/axios'

export default function GoogleConnect() {
  const [connected, setConnected]   = useState(null) // null=loading, true, false
  const [dismissed, setDismissed]   = useState(false)

  useEffect(() => {
    api.get('/auth/google/status')
      .then(res => setConnected(res.data.connected))
      .catch(() => setConnected(false))
  }, [])

  // Check URL param after OAuth redirect
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get('google') === 'connected') {
      setConnected(true)
      window.history.replaceState({}, '', '/chat')
    }
    if (params.get('google') === 'error') {
      window.history.replaceState({}, '', '/chat')
    }
  }, [])

  const handleConnect = () => {
    // Redirect browser to backend OAuth flow — token in header is sent automatically
    const token = localStorage.getItem('access_token')
    window.location.href = `http://127.0.0.1:8000/auth/google/connect?token=${token}`
  }

  const handleDisconnect = async () => {
    await api.delete('/auth/google/disconnect')
    setConnected(false)
  }

  if (connected === null || dismissed) return null

  if (connected) {
    return (
      <div className="flex items-center gap-2 px-4 py-2 bg-green-500/10 border-b border-green-500/20 text-xs text-green-400">
        <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
        Google account connected — email and calendar features are active.
        <button onClick={handleDisconnect} className="ml-auto text-slate-500 hover:text-red-400 transition">
          Disconnect
        </button>
        <button onClick={() => setDismissed(true)} className="text-slate-500 hover:text-white transition">
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3 px-4 py-2.5 bg-amber-500/10 border-b border-amber-500/20 text-xs">
      <svg className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      </svg>
      <span className="text-amber-300">
        Connect your Google account to use email and calendar features.
      </span>
      <button
        onClick={handleConnect}
        className="ml-auto flex-shrink-0 bg-white text-slate-800 font-medium px-3 py-1 rounded-lg hover:bg-slate-100 transition text-xs"
      >
        Connect Google
      </button>
      <button onClick={() => setDismissed(true)} className="text-slate-500 hover:text-white transition">
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  )
}
