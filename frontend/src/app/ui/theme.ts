import type { GlobalThemeOverrides } from 'naive-ui'

export function createTheme(dark: boolean, fonts = { ui:15, meta:13 }): GlobalThemeOverrides {
  return {
    common: {
      primaryColor: dark ? '#92baff' : '#2459b8',
      primaryColorHover: dark ? '#b8d1ff' : '#19468f',
      primaryColorPressed: dark ? '#92baff' : '#19468f',
      primaryColorSuppl: dark ? '#92baff' : '#2459b8',
      bodyColor: dark ? '#171b22' : '#f3f4f6',
      cardColor: dark ? '#202630' : '#ffffff',
      modalColor: dark ? '#202630' : '#ffffff',
      popoverColor: dark ? '#272f3b' : '#ffffff',
      textColorBase: dark ? '#edf1f7' : '#202b3a',
      textColor1: dark ? '#edf1f7' : '#202b3a',
      textColor2: dark ? '#b3bfce' : '#526174',
      textColor3: dark ? '#b3bfce' : '#526174',
      borderColor: dark ? '#465264' : '#dce1e8',
      fontSize: `${fonts.ui}px`, fontSizeSmall: `${fonts.ui}px`, fontSizeTiny: `${fonts.meta}px`,
      fontSizeMedium: `${fonts.ui}px`, fontSizeLarge: `${fonts.ui + 2}px`,
      lineHeight: '1.6',
      borderRadius: '10px',
      borderRadiusSmall: '7px',
      fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif',
    },
    Card: { borderRadius: '14px' },
    Tag: { fontSizeTiny: `${fonts.meta}px`, fontSizeSmall: `${fonts.meta}px`, fontSizeMedium: `${fonts.meta}px` },
    Button: { fontWeight: '500', fontSizeMedium: `${Math.max(14, fonts.ui - 1)}px`,
      fontSizeSmall: `${Math.max(14, fonts.ui - 1)}px`, fontSizeTiny: `${Math.max(14, fonts.ui - 1)}px`,
      textColorPrimary: dark ? '#14243c' : '#ffffff',
      textColorHoverPrimary: dark ? '#14243c' : '#ffffff',
      textColorPressedPrimary: dark ? '#14243c' : '#ffffff' },
  }
}
