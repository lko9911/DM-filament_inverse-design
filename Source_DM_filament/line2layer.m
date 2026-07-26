function La = line2layer(Li)
global Linfo
% Li = nx1 vector in line num
% La = nx1 vector in layer num

for i = 1:size(Li,1)
    for j=1:size(Linfo,1)
        if Li(i) <= Linfo(j) && Li(i) >= 1
            La(i) = j;
            break
        elseif Li(i) <1 || Li(i) >48
            La(i) = 0;
            break
        end
    end
end

end
