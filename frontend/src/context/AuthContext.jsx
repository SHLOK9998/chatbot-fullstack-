import React, { createContext, useContext, useEffect, useState } from 'react'
import api from '../api/axios'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser]       = useState(null)   // { user_id, username, display_name, email }
  const [loading, setLoading] = useState(true)   // true while checking /auth/me on load

  // On app load — check if cookie session is still valid
  useEffect(() => {
    const token = localStorage.getItem('access_token')
    if (!token) {
      setLoading(false)
      return
    }
    api.get('/auth/me')
      .then(res => setUser(res.data))
      .catch(() => {
        localStorage.removeItem('access_token')
        setUser(null)
      })
      .finally(() => setLoading(false))
  }, [])

  const login = async (username, password) => {
    const res = await api.post('/auth/login', { username, password })
    if (res.data.access_token) localStorage.setItem('access_token', res.data.access_token)
    setUser(res.data)
    return res.data
  }

  const register = async (username, email, password, display_name) => {
    const res = await api.post('/auth/register', { username, email, password, display_name })
    if (res.data.access_token) localStorage.setItem('access_token', res.data.access_token)
    setUser(res.data)
    return res.data
  }

  const logout = async () => {
    await api.post('/auth/logout')
    localStorage.removeItem('access_token')
    setUser(null)
  }

  const updateProfile = async (fields) => {
    const res = await api.put('/auth/me', fields)
    setUser(res.data)
    return res.data
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout, updateProfile }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
