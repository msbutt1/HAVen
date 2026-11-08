// The app runs the web dashboard's own adapter code (../dashboard/js), so Metro has to see that folder too.
const path = require('path');
const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);
config.watchFolders = [...(config.watchFolders || []), path.resolve(__dirname, '../dashboard/js')];
module.exports = config;
