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
    //
    // src/contract.ts is generated from admin/contract/ by `npm run gen:types`
    // (and gitignored): its shapes are the schemas', e.g. `{}` for the stream's
    // empty heartbeat data, so lint does not judge them.
    ignores: ['dist/', 'node_modules/', 'src/components/ui/**/*', 'src/contract.ts'],
  },
)
