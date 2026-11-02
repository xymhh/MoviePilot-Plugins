/**
 * 配置向导逻辑校验（v1.9.19）
 *
 * 直接从 Config.vue 里截取「配置向导」代码块，用假的 ref/computed/config/options 跑一遍：
 * 证明 3 步结构、网盘 schema 联动、校验条件、进第 3 步自动体检、完成即保存+复检。
 *
 * 跑法：node docs/test/2026-11-02-配置向导逻辑校验.mjs
 */
import fs from 'fs';

// 极简 ref/computed 模拟（同步求值即可）
const ref = (v) => ({value: v});
const computed = (fn) => ({get value() { return fn(); }});

const config = {
  enabled: true,
  cloud_drive: '115',
  organize_source_path: '/media/library',
  organize_after_transfer: true,
};
const options = {
  driverSchemas: [
    {id: '115', subtab: {value: '115'}, groups: [{title: '115 登录', icon: 'mdi-key', fields: [{key: 'p115_cookie', label: 'Cookie', type: 'text'}, {key: 'p115_root', label: '根目录', type: 'text'}]}]},
    {id: 'quark', subtab: {value: 'quark'}, groups: [{title: '夸克登录', fields: [{key: 'quark_cookie', label: 'Cookie', type: 'text'}]}]},
  ],
};
const sectionsValue = [
  {value: 'basic', groups: [{fields: [{key: 'enabled', type: 'switch'}, {key: 'cloud_drive', type: 'select'}]}]},
  {value: 'organize', groups: [{fields: [{key: 'organize_source_path', type: 'text'}, {key: 'organize_after_transfer', type: 'switch'}]}]},
];
const sections = {get value() { return sectionsValue; }};

const saving = ref(false);
let doctorCalls = 0, saveCalls = 0;
const api = {
  async get() { doctorCalls += 1; return {success: true, data: {counts: {ok: 7, warn: 1, error: 0}, items: [{key: 'a', status: 'ok', name: '私有库', detail: 'ok'}]}}; },
};
async function save() { saveCalls += 1; return true; }

const vue = fs.readFileSync(new URL('../../frontend/cloudsubscribex/src/components/Config.vue', import.meta.url), 'utf8');
const start = vue.indexOf('// ---------------------------------------------------------------- 配置向导');
const end = vue.indexOf('const navGroups = computed');
if (start < 0 || end < 0) throw new Error('Config.vue 里找不到向导代码块，检查标记');
const code = vue.slice(start, end);
const fn = new Function('ref', 'computed', 'config', 'options', 'sections', 'saving', 'api', 'save',
  code + '\nreturn {wizardMode, wizardStep, wizardSteps, currentWizardStep, wizardStepValid, toggleWizard, nextWizardStep, prevWizardStep, runWizardDoctor, finishWizard, wizardDoctor, wizardDoctorLoading, activeDriveSchema};');
const w = fn(ref, computed, config, options, sections, saving, api, save);

// 默认：专家模式
console.log('mode default:', w.wizardMode.value);
w.toggleWizard();
console.log('after toggle:', w.wizardMode.value, 'step', w.wizardStep.value);

// 第 1 步：基础字段 + 115 凭据组
const s1 = w.wizardSteps.value[0];
console.log('step1 groups:', s1.groups.map(g => `${g.title}(${g.fields.map(f => f.key).join(',')})`).join(' | '));
if (s1.groups.length !== 2) throw new Error('第1步应有 2 组');
if (!s1.groups[1].fields.some(f => f.key === 'p115_cookie')) throw new Error('未带出 115 凭据');
if (!w.wizardStepValid.value) throw new Error('有网盘时第1步应通过');

// 切到夸克：schema 跟着变
config.cloud_drive = 'quark';
console.log('quark groups:', w.wizardSteps.value[0].groups.map(g => g.fields.map(f => f.key).join(',')).join(' | '));
if (!w.wizardSteps.value[0].groups.some(g => g.fields.some(f => f.key === 'quark_cookie'))) throw new Error('切换网盘后未换 schema');

// 清空网盘 → 第1步不通过
config.cloud_drive = '';
if (w.wizardStepValid.value) throw new Error('没选网盘时第1步不应通过');
config.cloud_drive = '115';

// 第 2 步
w.nextWizardStep();
console.log('step2:', w.currentWizardStep.value.title, w.wizardSteps.value[1].groups[0].fields.map(f => f.key).join(','));
if (w.wizardStep.value !== 1) throw new Error('未进入第2步');

// 第 3 步：自动体检
await w.nextWizardStep();
console.log('step3:', w.currentWizardStep.value.title, 'groups', w.wizardSteps.value[2].groups.length, 'doctor', JSON.stringify(w.wizardDoctor.value?.counts));
if (w.wizardStep.value !== 2) throw new Error('未进入第3步');
if (!w.wizardDoctor.value) throw new Error('进第3步未自动体检');
if (doctorCalls !== 1) throw new Error('体检调用次数异常 ' + doctorCalls);

// 完成并保存
await w.finishWizard();
console.log('after finish: saveCalls', saveCalls, 'doctorCalls', doctorCalls);
if (saveCalls !== 1 || doctorCalls !== 2) throw new Error('完成流程调用次数异常');

// 回退
w.prevWizardStep();
if (w.wizardStep.value !== 1) throw new Error('回退失败');
console.log('PASS 向导逻辑全部通过');
