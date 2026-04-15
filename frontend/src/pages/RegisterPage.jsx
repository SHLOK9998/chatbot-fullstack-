import React, { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function RegisterPage() {
  const { register } = useAuth()
  const navigate     = useNavigate()
  const [form, setForm]     = useState({ username: '', email: '', password: '', display_name: '' })
  const [errors, setErrors] = useState([])
  const [loading, setLoading] = useState(false)

  const handleChange = e => setForm(f => ({ ...f, [e.target.name]: e.target.value }))

  const handleSubmit = async e => {
    e.preventDefault()
    setErrors([])
    setLoading(true)
    try {
      await register(form.username, form.email, form.password, form.display_name)
      navigate('/chat')
    } catch (err) {
      const detail = err.response?.data?.detail
      if (Array.isArray(detail)) setErrors(detail)
      else setErrors([detail || 'Registration failed. Please try again.'])
    } finally {
      setLoading(false)
    }
  }

  const fields = [
    { name: 'display_name', label: 'Display Name',    type: 'text',     placeholder: 'Your full name' },
    { name: 'username',     label: 'Username',         type: 'text',     placeholder: '3–30 chars, letters/numbers/underscore' },
    { name: 'email',        label: 'Email',            type: 'email',    placeholder: 'you@example.com' },
    { name: 'password',     label: 'Password',         type: 'password', placeholder: 'Min 8 chars, 1 letter + 1 number' },
  ]

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center p-4">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-brand-500 mb-4 text-3xl">🤖</div>
          <h1 className="text-2xl font-bold text-white">Create Account</h1>
          <p className="text-slate-400 mt-1 text-sm">Join your Personal AI Assistant</p>
        </div>

        {/* Card */}
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 shadow-2xl">
          <form onSubmit={handleSubmit} className="space-y-4">
            {fields.map(f => (
              <div key={f.name}>
                <label className="block text-sm font-medium text-slate-300 mb-1.5">{f.label}</label>
                <input
                  name={f.name}
                  type={f.type}
                  value={form[f.name]}
                  onChange={handleChange}
                  required
                  autoFocus={f.name === 'display_name'}
                  placeholder={f.placeholder}
                  className="w-full bg-slate-800 border border-slate-700 text-white placeholder-slate-500 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent transition"
                />
              </div>
            ))}

            {errors.length > 0 && (
              <div className="bg-red-500/10 border border-red-500/30 text-red-400 text-sm rounded-xl px-4 py-3 space-y-1">
                {errors.map((e, i) => <p key={i}>• {e}</p>)}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-brand-500 hover:bg-brand-600 disabled:opacity-60 disabled:cursor-not-allowed text-white font-semibold rounded-xl py-3 text-sm transition flex items-center justify-center gap-2 mt-2"
            >
              {loading ? (
                <><div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" /> Creating account...</>
              ) : 'Create Account'}
            </button>
          </form>

          <p className="text-center text-slate-400 text-sm mt-6">
            Already have an account?{' '}
            <Link to="/login" className="text-brand-500 hover:text-brand-600 font-medium transition">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
