import json

data = json.load(open('sample_info.json'))
voxels = data['voxels']

print(f'Total voxels: {len(voxels)}')
print(f'Max layer_num: {max(v["layer_num"] for v in voxels)}')
print(f'Unique layers: {len(set(v["layer_num"] for v in voxels))}')
print()
print('First 5 voxels:')
for v in voxels[:5]:
    print(f'  Voxel {v["voxel_id"]}: layer={v["layer_num"]}, E={v["voxel_filament_e_mm"]:.4f}')
print()
print('Last 5 voxels:')
for v in voxels[-5:]:
    print(f'  Voxel {v["voxel_id"]}: layer={v["layer_num"]}, E={v["voxel_filament_e_mm"]:.4f}')
