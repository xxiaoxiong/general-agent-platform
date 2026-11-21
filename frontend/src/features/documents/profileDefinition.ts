import type { ProfileDefinition, ProfileFormatting, ProfileInput, ProfileSection } from '../../api/documents'

export function defaultFormatting(): ProfileFormatting {
  return {
    page_size: 'A4', margins_cm: { top: 2.54, right: 2.54, bottom: 2.54, left: 2.54 },
    heading_hierarchy: true, tables_allowed: true, font_family: '微软雅黑',
    body_font_size: 11, line_spacing: 1.35, header: '', footer: '', page_numbers: true, section_page_break: false,
  }
}

export function createProfileSection(index: number, id = `section_${index + 1}`): ProfileSection {
  return {
    id, title: '', order: index + 1, level: 1, required: true, purpose: '',
    required_fields: [], writing_guidance: '', source_requirements: [],
  }
}

export function createProfileDefinition(): ProfileDefinition {
  return {
    schema_version: 1, document_type: 'report', name: '', description: '',
    language: 'zh-CN', audience: '', tone: '', output_formats: ['docx'],
    sections: [createProfileSection(0)], required_inputs: [],
    global_rules: [], formatting: defaultFormatting(),
    quality_rules: [],
  }
}

export function nextProfileSection(sections: ProfileSection[]): ProfileSection {
  let nextId = sections.length + 1
  while (sections.some(section => section.id === `section_${nextId}`)) nextId++
  return createProfileSection(sections.length, `section_${nextId}`)
}

export function normalizeProfileSections(sections: ProfileSection[]): ProfileSection[] {
  return sections.map((section, index) => ({ ...section, title: section.title.trim(), order: index + 1 }))
}

export function nextProfileInput(inputs: ProfileInput[]): ProfileInput {
  let nextId = inputs.length + 1
  while (inputs.some(field => field.id === `field_${nextId}`)) nextId++
  return { id: `field_${nextId}`, label: '', type: 'text', required: true, description: '', options: [] }
}
