import { describe, expect, it } from 'vitest'

import { extractSpeakBlocks, sanitizeTextForSpeech } from './speech-text'

describe('extractSpeakBlocks', () => {
  it('returns null when no markers are present', () => {
    expect(extractSpeakBlocks('plain answer with **bold**')).toBeNull()
  })

  it('extracts HTML comment speak blocks', () => {
    const text = [
      '## Long answer',
      '',
      '| a | b |',
      '| - | - |',
      '| 1 | 2 |',
      '',
      '<!-- speak -->',
      'Only this recap should be spoken.',
      '<!-- /speak -->'
    ].join('\n')

    expect(extractSpeakBlocks(text)).toBe('Only this recap should be spoken.')
  })

  it('accepts compact comment whitespace variants case-insensitively', () => {
    const text = 'body\n<!--SPEAK-->Recap here.<!--/SPEAK-->\ntail'
    expect(extractSpeakBlocks(text)).toBe('Recap here.')
  })

  it('extracts fenced speak blocks', () => {
    const text = ['Details…', '', '```speak', 'Fence recap.', '```', ''].join('\n')
    expect(extractSpeakBlocks(text)).toBe('Fence recap.')
  })

  it('concatenates multiple blocks in document order', () => {
    const text = [
      '<!-- speak -->First.<!-- /speak -->',
      'middle junk',
      '```speak',
      'Second.',
      '```'
    ].join('\n')

    expect(extractSpeakBlocks(text)).toBe('First. Second.')
  })

  it('returns null when every speak block is empty (fall through)', () => {
    expect(extractSpeakBlocks('<!-- speak -->\n\n<!-- /speak -->')).toBeNull()
    expect(extractSpeakBlocks('```speak\n\n```')).toBeNull()
  })
})

describe('sanitizeTextForSpeech', () => {
  it('sanitizes full text when no speak markers exist', () => {
    expect(sanitizeTextForSpeech('This is **bold** and `code`.')).toBe('This is bold and code.')
  })

  it('speaks only the speak block when present; ignores tables and bullets', () => {
    const text = [
      '## Details',
      '',
      '- one',
      '- two',
      '',
      '| col |',
      '| --- |',
      '| x |',
      '',
      '```python',
      'print("nope")',
      '```',
      '',
      '<!-- speak -->',
      'Short takeaway for the ear.',
      '<!-- /speak -->'
    ].join('\n')

    const spoken = sanitizeTextForSpeech(text)
    expect(spoken).toBe('Short takeaway for the ear.')
    expect(spoken).not.toMatch(/one|two|print|Details|col/i)
  })

  it('prefers speak fence over other body fences', () => {
    const text = [
      '```js',
      'console.log(1)',
      '```',
      '```speak',
      'Only fence recap.',
      '```'
    ].join('\n')

    expect(sanitizeTextForSpeech(text)).toBe('Only fence recap.')
  })

  it('falls through to full sanitize when speak blocks are empty', () => {
    const text = 'Hello **world**\n\n<!-- speak -->\n<!-- /speak -->'
    expect(sanitizeTextForSpeech(text)).toBe('Hello world.')
  })
})
