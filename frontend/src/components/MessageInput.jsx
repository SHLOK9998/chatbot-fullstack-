// import React, { useState, useRef, useEffect } from 'react'

// export default function MessageInput({ onSend, disabled }) {
//   const [text, setText] = useState('')
//   const textareaRef     = useRef(null)

//   // Keep focus on textarea always
//   useEffect(() => {
//     if (!disabled) textareaRef.current?.focus()
//   }, [disabled])

//   // Auto-resize textarea
//   useEffect(() => {
//     const el = textareaRef.current
//     if (!el) return
//     el.style.height = 'auto'
//     el.style.height = Math.min(el.scrollHeight, 160) + 'px'
//   }, [text])

//   const handleSend = () => {
//     const trimmed = text.trim()
//     if (!trimmed || disabled) return
//     onSend(trimmed)
//     setText('')
//     if (textareaRef.current) {
//       textareaRef.current.style.height = 'auto'
//       textareaRef.current.focus()
//     }
//   }

//   const handleKeyDown = e => {
//     if (e.key === 'Enter' && !e.shiftKey) {
//       e.preventDefault()
//       handleSend()
//     }
//   }

//   return (
//     <div className="px-4 py-4 border-t border-slate-800 flex-shrink-0">
//       <div className="max-w-3xl mx-auto">
//         <div className={`flex items-end gap-3 bg-slate-950 border rounded-2xl px-4 py-3 transition ${
//           disabled ? 'border-slate-800 opacity-60' : 'border-slate-800 focus-within:border-brand-500'
//         }`}>
//           <textarea
//             ref={textareaRef}
//             value={text}
//             onChange={e => setText(e.target.value)}
//             onKeyDown={handleKeyDown}
//             disabled={disabled}
//             autoFocus
//             placeholder="how can i help you today?"
//             rows={1}
//             className="flex-1 bg-transparent text-white placeholder-slate-500 text-sm resize-none focus:outline-none leading-relaxed"
//             style={{ maxHeight: '160px' }}
//           />
//           <button
//             onClick={handleSend}
//             disabled={disabled || !text.trim()}
//             className="flex-shrink-0 w-8 h-8 rounded-xl bg-brand-500 hover:bg-brand-600 disabled:opacity-40 disabled:cursor-not-allowed text-white flex items-center justify-center transition"
//           >
//             {disabled ? (
//               <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
//             ) : (
//               <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
//                 <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
//               </svg>
//             )}
//           </button>
//         </div>
//       </div>
//     </div>
//   )
// }



import React, { useState, useRef, useEffect } from 'react'

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function MessageInput({
  onSend,
  disabled,
  attachments = [],
  onUploadAttachment,
  onRemoveAttachment,
}) {
  const [text, setText]           = useState('')
  const [uploading, setUploading] = useState(false)
  const textareaRef               = useRef(null)
  const fileInputRef              = useRef(null)

  // Keep focus on textarea always
  useEffect(() => {
    if (!disabled) textareaRef.current?.focus()
  }, [disabled])

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
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.focus()
    }
  }

  const handleKeyDown = e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // ── File picker ─────────────────────────────────────────────────────────────
  const handlePaperclipClick = () => {
    if (disabled || uploading) return
    fileInputRef.current?.click()
  }

  const handleFileChange = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    // Reset so the same file can be re-selected after removal
    e.target.value = ''

    if (!onUploadAttachment) return
    setUploading(true)
    try {
      await onUploadAttachment(file)
    } finally {
      setUploading(false)
      textareaRef.current?.focus()
    }
  }

  return (
    <div className="px-4 py-4 border-t border-slate-800 flex-shrink-0">
      <div className="max-w-3xl mx-auto">

        {/* ── Attachment badges ─────────────────────────────────────────────── */}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {attachments.map(att => (
              <div
                key={att.filename}
                className="flex items-center gap-1.5 bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-300"
              >
                {/* File icon */}
                <svg className="w-3.5 h-3.5 text-brand-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
                </svg>
                <span className="max-w-[140px] truncate">{att.filename}</span>
                <span className="text-slate-500 flex-shrink-0">{formatBytes(att.size)}</span>
                {/* Remove button */}
                <button
                  onClick={() => onRemoveAttachment?.(att.filename)}
                  className="ml-0.5 text-slate-500 hover:text-red-400 transition flex-shrink-0"
                  title="Remove attachment"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        )}

        {/* ── Input row ─────────────────────────────────────────────────────── */}
        <div className={`flex items-end gap-3 bg-slate-950 border rounded-2xl px-4 py-3 transition ${
          disabled ? 'border-slate-800 opacity-60' : 'border-slate-800 focus-within:border-brand-500'
        }`}>

          {/* Paperclip button */}
          <button
            onClick={handlePaperclipClick}
            disabled={disabled || uploading}
            title="Attach a file"
            className="flex-shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-slate-500
                       hover:text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            {uploading ? (
              <div className="w-3.5 h-3.5 border-2 border-slate-400 border-t-transparent rounded-full animate-spin" />
            ) : (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
              </svg>
            )}
          </button>

          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={handleFileChange}
          />

          {/* Textarea */}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled}
            autoFocus
            placeholder="how can i help you today?"
            rows={1}
            className="flex-1 bg-transparent text-white placeholder-slate-500 text-sm resize-none focus:outline-none leading-relaxed"
            style={{ maxHeight: '160px' }}
          />

          {/* Send button */}
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

      </div>
    </div>
  )
}