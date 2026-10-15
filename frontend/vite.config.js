import {defineConfig} from 'vite';
export default defineConfig({resolve:{preserveSymlinks:true},server:{proxy:{'/api':{target:'http://127.0.0.1:8950',ws:true}}},test:{environment:'jsdom',globals:true}});
