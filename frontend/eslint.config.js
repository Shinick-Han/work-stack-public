import tseslint from 'typescript-eslint'

export default [
  {
    ignores: ['dist/**', 'coverage/**', 'e2e/**', '**/*.test.ts', '**/*.test.tsx', 'src/test/**'],
  },
  {
    files: ['src/**/*.ts', 'src/**/*.tsx'],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
        ecmaVersion: 'latest',
        sourceType: 'module',
      },
    },
    rules: {
      complexity: ['warn', { max: 15, variant: 'modified' }],
      'max-depth': ['warn', 4],
      'max-lines-per-function': ['warn', {
        IIFEs: true,
        max: 100,
        skipBlankLines: true,
        skipComments: true,
      }],
    },
  },
]
