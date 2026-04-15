import React, { useState, useRef, useEffect } from 'react'

export default function MessageInput({ onSend, disabled }) {
  const [text, setText] = useState('')
  const textareaRef     = useRef(null)

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [text])

  const handleSend = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
    // Reset height
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  const handleKeyDown = e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="px-4 py-4 border-t border-slate-800 bg-slate-900/50 backdrop-blur-sm flex-shrink-0">
      <div className="max-w-3xl mx-auto">
        <div className={`flex items-end gap-3 bg-slate-800 border rounded-2xl px-4 py-3 transition ${
          disabled ? 'border-slate-700 opacity-60' : 'border-slate-700 focus-within:border-brand-500'
        }`}>
          <textarea
            ref={textareaRef}
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled}
            placeholder="Message AI Assistant... (Shift+Enter for new line)"
            rows={1}
            className="flex-1 bg-transparent text-white placeholder-slate-500 text-sm resize-none focus:outline-none leading-relaxed"
            style={{ maxHeight: '160px' }}
          />
          <button
            onClick={handleSend}
            disabled={disabled || !text.trim()}
            className="flex-shrink-0 w-8 h-8 rounded-xl bg-brand-500 hover:bg-brand-600 disabled:opacity-40 disabled:cursor-not-allowed text-white flex items-center justify-center transition"
          >
            {disabled ? (
              <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            )}
          </button>
        </div>
        <p className="text-xs text-slate-600 text-center mt-2">
          Press Enter to send · Shift+Enter for new line
        </p>
      </div>
    </div>
  )
}
