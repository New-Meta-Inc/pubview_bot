export interface HouseDef {
  id: string;
  name: string;
  emoji: string;
  themeBg: string;
  themeRing: string;
  themeAccent: string;
  themeBar: string;
}

export const HOUSES: HouseDef[] = [
  { id: 'raptor', name: 'ラプター', emoji: '🦖', themeBg: 'bg-raptor-dark/40', themeRing: 'ring-raptor/40', themeAccent: 'text-raptor', themeBar: 'bg-raptor' },
  { id: 'krug',   name: 'クルーグ', emoji: '🪨', themeBg: 'bg-krug-dark/40',   themeRing: 'ring-krug/40',   themeAccent: 'text-krug',   themeBar: 'bg-krug' },
  { id: 'wolf',   name: 'ウルフ',   emoji: '🐺', themeBg: 'bg-wolf-dark/40',   themeRing: 'ring-wolf/40',   themeAccent: 'text-wolf',   themeBar: 'bg-wolf' },
  { id: 'gromp',  name: 'グロンプ', emoji: '🐸', themeBg: 'bg-gromp-dark/40',  themeRing: 'ring-gromp/40',  themeAccent: 'text-gromp',  themeBar: 'bg-gromp' },
];

export const HOUSE_LEVEL_COEF = 300;
export const HOUSE_LEVEL_EXP = 2.0;
export const USER_LEVEL_COEF = 20;
export const USER_LEVEL_EXP = 2.0;

export function levelFromXp(xp: number, coef: number, exp: number): number {
  if (xp <= 0) return 1;
  return Math.max(1, Math.floor((xp / coef) ** (1.0 / exp)));
}

export function requiredXpForLevel(level: number, coef: number, exp: number): number {
  if (level <= 1) return 0;
  return Math.floor(coef * level ** exp);
}

export function houseById(id: string): HouseDef | undefined {
  return HOUSES.find((h) => h.id === id);
}
