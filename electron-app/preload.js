const fs = require('fs');
const path = require('path');

try {
    const configPath = path.join(__dirname, 'config.json');
    const config = JSON.parse(fs.readFileSync(configPath));

    localStorage.setItem('TENANT', config.tenant);
    localStorage.setItem('GYM_BRANCH_ID', config.gymBranchId);
    localStorage.setItem('realm', config.tenant);
    localStorage.setItem('currentGymBranchId', config.gymBranchId);
} catch (error) {
    console.error(' Failed to inject config into storage:', error);
}
