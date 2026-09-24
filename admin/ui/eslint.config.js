import eslint from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['src/**/*.{ts,vue}'],
    languageOptions: {
      globals: {
        document: 'readonly',
        window: 'readonly',
      },
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    // Generated shadcn-vue components may use conventions this project does
    // not enforce (numeric spacing, stock type scale). They are owned by the
    // shadcn layer, not app code, so lint skips them.
    ignores: ['dist/', 'node_modules/', 'src/components/ui/**/*'],
  },
)
