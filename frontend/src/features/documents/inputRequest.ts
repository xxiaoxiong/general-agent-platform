import type { InputRequest } from '../../api/documents'

export function visibleInputFields(request: InputRequest) {
  const required = new Set([
    ...(request.input_schema.required || []),
    ...(request.required_fields || []),
  ])
  return Object.entries(request.input_schema.properties || {}).map(([key, schema]) => ({
    key, schema, required: required.has(key),
  }))
}

export function isProfileConfirmation(request: InputRequest) {
  const fields = request.input_schema.properties || {}
  return fields.definition?.type === 'object' && fields.confirm?.type === 'boolean'
}

export function collectRunInput(request: InputRequest, raw: Record<string, unknown>) {
  const values: Record<string, unknown> = Object.create(null)
  for (const { key, schema, required } of visibleInputFields(request)) {
    const rawValue = raw[key]
    const label = schema.title || key
    const missing = rawValue === undefined || rawValue === '' || rawValue === null
      || (Array.isArray(rawValue) && !rawValue.length)
    if (missing) {
      if (required) throw new Error(`请填写${label}。`)
      continue
    }
    if (schema.enum) {
      const index = Number(rawValue)
      if (!Number.isInteger(index) || index < 0 || index >= schema.enum.length) throw new Error(`请选择有效的${label}。`)
      values[key] = schema.enum[index]
      continue
    }
    if (schema.type === 'array') {
      const choices = schema.items?.enum || []
      if (!Array.isArray(rawValue) || rawValue.some(index => !Number.isInteger(index) || index < 0 || index >= choices.length)) {
        throw new Error(`请选择有效的${label}。`)
      }
      values[key] = rawValue.map(index => choices[index])
      continue
    }
    if (schema.type === 'number' || schema.type === 'integer') {
      const value = Number(rawValue)
      if (!Number.isFinite(value) || (schema.type === 'integer' && !Number.isInteger(value))) {
        throw new Error(`${label}需要填写${schema.type === 'integer' ? '整数' : '数字'}。`)
      }
      values[key] = value
      continue
    }
    if (schema.type === 'boolean') {
      values[key] = rawValue === 'true'
      continue
    }
    if (schema.type === 'object') {
      try {
        const value = JSON.parse(String(rawValue))
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error()
        values[key] = value
      } catch {
        throw new Error(`${label}需要填写有效的 JSON 对象。`)
      }
      continue
    }
    const value = String(rawValue).trim()
    if (required && !value) throw new Error(`请填写${label}。`)
    if (value) values[key] = value
  }
  return values
}
